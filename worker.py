#!/usr/bin/env python3
"""
Distributed Caption Worker
===========================
Polls the coordinator for parquet shards, captions all images with Molmo 7B,
returns JSONL results, and keeps minimal local disk usage (one shard at a time).

Runs on macOS (MPS), Linux (CUDA), or CPU-only machines.

Usage:
    python worker.py --coordinator http://10.0.0.1:5000 \
                     --worker-name  mac-studio-1

The worker loops forever — use Ctrl+C or the LaunchAgent to manage it.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
import tempfile
import time
from pathlib import Path

import traceback

import requests
import torch
from PIL import Image
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoProcessor, GenerationConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

MODEL_ID        = "allenai/Molmo-7B-D-0924"
POLL_INTERVAL   = 30    # seconds between polls when queue is empty
HEARTBEAT_EVERY = 45    # seconds between heartbeats during processing
CAPTION_PROMPT  = "Describe this image in detail."


# ── Device detection ─────────────────────────────────────────────────────────

def _best_device() -> tuple[str, torch.dtype]:
    if torch.cuda.is_available():
        return "cuda", torch.bfloat16
    if torch.backends.mps.is_available():
        # bfloat16 not fully supported on older MPS — use float16
        return "mps", torch.float16
    return "cpu", torch.float32


# ── Molmo model ──────────────────────────────────────────────────────────────

def _load_model(device: str, dtype: torch.dtype):
    log.info("Loading Molmo 7B on %s (%s) …", device, dtype)
    cache_dir = os.environ.get("HF_HOME", None)

    processor = AutoProcessor.from_pretrained(
        MODEL_ID, trust_remote_code=True, cache_dir=cache_dir)

    # MPS / CPU: don't use device_map="auto" (requires accelerate CUDA dispatch)
    if device in ("mps", "cpu"):
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, trust_remote_code=True,
            torch_dtype=dtype, cache_dir=cache_dir,
        ).to(device).eval()
    else:
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, trust_remote_code=True,
            torch_dtype=dtype, device_map="auto", cache_dir=cache_dir,
        ).eval()

    log.info("Model ready.")
    return model, processor


def _caption_image(
    img: Image.Image,
    model,
    processor,
    device: str,
    dtype: torch.dtype,
) -> str:
    inputs = processor.process(images=[img], text=CAPTION_PROMPT)
    inputs = {
        k: (
            (v.to(device, dtype=dtype) if v.is_floating_point() else v.to(device))
            .unsqueeze(0)
        ) if isinstance(v, torch.Tensor) else v
        for k, v in inputs.items()
    }
    with torch.no_grad():
        output = model.generate_from_batch(
            inputs,
            GenerationConfig(max_new_tokens=300, stop_strings="<|endoftext|>"),
            tokenizer=processor.tokenizer,
        )
    return processor.tokenizer.decode(output[0], skip_special_tokens=True).strip()


# ── Parquet streaming ─────────────────────────────────────────────────────────

def _iter_images(parquet_path: str):
    """Yield (uid, caption_text, PIL.Image) from a parquet file."""
    import pyarrow.parquet as pq

    COLS = ["id", "caption", "image"]
    pf = pq.ParquetFile(parquet_path)
    for batch in pf.iter_batches(batch_size=500, columns=COLS):
        for i in range(len(batch)):
            uid     = batch.column("id")[i].as_py()
            caption = batch.column("caption")[i].as_py() or ""
            img_bytes = batch.column("image")[i]["bytes"].as_py()
            if not img_bytes:
                continue
            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                yield uid, caption, img
            except Exception:
                pass


# ── Worker loop ───────────────────────────────────────────────────────────────

def _download_shard(coordinator: str, shard_id: str, dest: Path) -> bool:
    url = f"{coordinator}/download/{shard_id}"
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            with open(dest, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True,
                desc=f"↓ {Path(shard_id).name}", leave=False,
            ) as bar:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    bar.update(len(chunk))
        return True
    except Exception as e:
        log.error("Download failed: %s", e)
        return False


def _submit_results(
    coordinator: str, shard_id: str, worker_name: str, lines: list[str]
) -> bool:
    body = "\n".join(lines) + "\n"
    url  = f"{coordinator}/submit/{shard_id}?worker={worker_name}"
    try:
        r = requests.post(url, data=body.encode(), timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Submit failed: %s", e)
        return False


def _heartbeat(coordinator: str, worker_name: str) -> None:
    try:
        requests.post(f"{coordinator}/heartbeat?worker={worker_name}", timeout=5)
    except Exception:
        pass


def run_worker(coordinator: str, worker_name: str) -> None:
    device, dtype = _best_device()
    log.info("Worker %s | device=%s | dtype=%s", worker_name, device, dtype)

    model, processor = _load_model(device, dtype)

    while True:
        # Poll for work
        try:
            r = requests.get(
                f"{coordinator}/get_task?worker={worker_name}", timeout=10)
            shard_id = r.json().get("shard_id")
        except Exception as e:
            log.warning("Coordinator unreachable: %s — retrying in %ds", e, POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
            continue

        if shard_id is None:
            log.info("Queue empty — waiting %ds …", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
            continue

        log.info("Got task: %s", shard_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            shard_path = Path(tmpdir) / Path(shard_id).name

            # Download
            if not _download_shard(coordinator, shard_id, shard_path):
                time.sleep(10)
                continue

            # Caption
            lines: list[str] = []
            last_hb = time.time()

            for uid, orig_caption, img in _iter_images(str(shard_path)):
                try:
                    caption = _caption_image(img, model, processor, device, dtype)
                except Exception as e:
                    log.warning("Caption error for %s: %s\n%s", uid, e, traceback.format_exc())
                    caption = orig_caption   # fall back to original

                lines.append(json.dumps({"key": uid, "caption": caption}))

                # Heartbeat so coordinator knows we're alive
                if time.time() - last_hb > HEARTBEAT_EVERY:
                    _heartbeat(coordinator, worker_name)
                    last_hb = time.time()

            log.info("Captioned %d images in %s", len(lines), shard_id)

        # shard_path is deleted here (tmpdir cleanup)

        # Submit
        if lines:
            _submit_results(coordinator, shard_id, worker_name, lines)
        else:
            log.warning("No captions produced for %s — skipping submit", shard_id)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Distributed caption worker")
    p.add_argument("--coordinator", required=True,
                   help="Coordinator URL, e.g. http://10.0.0.1:5000")
    p.add_argument("--worker-name", default=None,
                   help="Unique name for this worker (default: hostname)")
    args = p.parse_args()

    worker_name = args.worker_name or socket.gethostname()
    run_worker(args.coordinator, worker_name)


if __name__ == "__main__":
    main()
