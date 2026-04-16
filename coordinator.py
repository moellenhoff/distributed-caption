#!/usr/bin/env python3
"""
Distributed Caption Coordinator
================================
Manages a queue of parquet shards, assigns them to workers on demand,
and collects JSONL caption results.

Workers connect over HTTP — no SSH keys, no shared filesystem required.

Usage:
    python coordinator.py --parquet-dir /data/raw/pd12m \
                          --output-dir  /data/captions \
                          --port 5000
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, stream_with_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("coordinator")

TASK_TIMEOUT_SEC = 30 * 60   # 30 min — shard returns to queue if worker disappears
HEARTBEAT_SEC    = 60        # workers send heartbeat every 60 s

app   = Flask(__name__)
state_lock = threading.Lock()

# ── State ────────────────────────────────────────────────────────────────────
#
# shards: { shard_id: {status, worker, started_at, completed_at, count} }
# workers: { worker_name: {last_seen, current_shard} }

state: dict = {"shards": {}, "workers": {}}
state_path: Path | None  = None
parquet_dir: Path | None = None
output_dir:  Path | None = None


def _save_state() -> None:
    """Atomically persist state to disk."""
    if state_path is None:
        return
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


def _shard_path(shard_id: str) -> Path:
    assert parquet_dir is not None
    return parquet_dir / shard_id


def _output_path(shard_id: str) -> Path:
    assert output_dir is not None
    stem = Path(shard_id).stem
    return output_dir / f"{stem}.jsonl"


def _reclaim_timed_out() -> None:
    """Reset in-progress shards whose worker has gone silent."""
    now = time.time()
    for shard_id, info in state["shards"].items():
        if info["status"] == "in_progress":
            if now - info.get("started_at", now) > TASK_TIMEOUT_SEC:
                log.warning("Timeout: reclaiming %s from %s", shard_id, info.get("worker"))
                state["shards"][shard_id]["status"] = "queued"
                state["shards"][shard_id]["worker"]  = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    with state_lock:
        _reclaim_timed_out()
        counts = {"queued": 0, "in_progress": 0, "done": 0}
        for s in state["shards"].values():
            counts[s["status"]] += 1
        return jsonify({
            "shards":  counts,
            "total":   sum(counts.values()),
            "workers": state["workers"],
        })


@app.get("/get_task")
def get_task():
    worker_name = request.args.get("worker", "unknown")
    with state_lock:
        _reclaim_timed_out()
        # Register / update worker heartbeat
        state["workers"][worker_name] = {
            "last_seen":    time.time(),
            "current_shard": state["workers"].get(worker_name, {}).get("current_shard"),
        }
        # Find next queued shard
        for shard_id, info in state["shards"].items():
            if info["status"] == "queued":
                state["shards"][shard_id].update({
                    "status":     "in_progress",
                    "worker":     worker_name,
                    "started_at": time.time(),
                })
                state["workers"][worker_name]["current_shard"] = shard_id
                _save_state()
                log.info("Assigned %s → %s", shard_id, worker_name)
                return jsonify({"shard_id": shard_id})

    return jsonify({"shard_id": None})   # no work available


@app.get("/download/<path:shard_id>")
def download_shard(shard_id: str):
    """Stream a parquet file to the worker."""
    path = _shard_path(shard_id)
    if not path.exists():
        return Response("not found", status=404)

    def _stream():
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):   # 1 MB chunks
                yield chunk

    return Response(
        stream_with_context(_stream()),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                 "Content-Length": str(path.stat().st_size)},
    )


@app.post("/submit/<path:shard_id>")
def submit(shard_id: str):
    """Receive JSONL captions from worker and mark shard done."""
    worker_name = request.args.get("worker", "unknown")
    data        = request.get_data()
    if not data:
        return Response("empty body", status=400)

    out = _output_path(shard_id)
    out.write_bytes(data)
    count = data.count(b"\n")

    with state_lock:
        if shard_id in state["shards"]:
            state["shards"][shard_id].update({
                "status":       "done",
                "completed_at": time.time(),
                "count":        count,
            })
        if worker_name in state["workers"]:
            state["workers"][worker_name]["current_shard"] = None
        _save_state()

    log.info("Done: %s (%d captions) from %s", shard_id, count, worker_name)
    return jsonify({"ok": True, "count": count})


@app.post("/heartbeat")
def heartbeat():
    worker_name = request.args.get("worker", "unknown")
    with state_lock:
        if worker_name in state["workers"]:
            state["workers"][worker_name]["last_seen"] = time.time()
    return jsonify({"ok": True})


# ── Startup ───────────────────────────────────────────────────────────────────

def _init_state(pq_dir: Path, out_dir: Path, state_file: Path) -> None:
    global state, state_path, parquet_dir, output_dir
    parquet_dir = pq_dir
    output_dir  = out_dir
    state_path  = state_file
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load existing state if present
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            log.info("Loaded state: %d shards", len(state["shards"]))
        except Exception as e:
            log.warning("Could not load state: %s — rebuilding", e)

    # Add any new parquets not yet in state
    known = set(state["shards"].keys())
    for pq in sorted(pq_dir.glob("**/*.parquet")):
        shard_id = str(pq.relative_to(pq_dir))
        if shard_id in known:
            continue
        # Skip if output already exists
        if _output_path(shard_id).exists():
            state["shards"][shard_id] = {"status": "done", "worker": None,
                                          "started_at": None, "completed_at": None}
        else:
            state["shards"][shard_id] = {"status": "queued", "worker": None,
                                          "started_at": None, "completed_at": None}

    queued = sum(1 for s in state["shards"].values() if s["status"] == "queued")
    done   = sum(1 for s in state["shards"].values() if s["status"] == "done")
    log.info("Queue: %d queued, %d already done", queued, done)
    _save_state()


def main() -> None:
    p = argparse.ArgumentParser(description="Distributed caption coordinator")
    p.add_argument("--parquet-dir", required=True)
    p.add_argument("--output-dir",  required=True)
    p.add_argument("--port",        type=int, default=5000)
    p.add_argument("--host",        default="0.0.0.0")
    p.add_argument("--state-file",  default=None,
                   help="Path to state JSON (default: output-dir/coordinator_state.json)")
    args = p.parse_args()

    pq_dir     = Path(args.parquet_dir)
    out_dir    = Path(args.output_dir)
    state_file = Path(args.state_file) if args.state_file else out_dir / "coordinator_state.json"

    _init_state(pq_dir, out_dir, state_file)
    log.info("Coordinator listening on %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
