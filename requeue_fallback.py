#!/usr/bin/env python3
"""
Detect shards with fallback (non-Molmo) captions and re-queue them.

Molmo output always starts with "User: Describe this image in detail. Assistant:"
Original pd12m captions do not have this prefix → those shards need re-captioning.

Usage (coordinator must be stopped first):
    python requeue_fallback.py --output-dir /path/to/molmo_captions_track_b --dry-run
    python requeue_fallback.py --output-dir /path/to/molmo_captions_track_b
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MOLMO_PREFIX = "User: Describe this image in detail."


def is_molmo(jsonl_path: Path) -> bool:
    """Return True if the first caption in the JSONL looks like real Molmo output."""
    try:
        with open(jsonl_path) as f:
            first_line = f.readline()
        caption = json.loads(first_line).get("caption", "")
        return caption.startswith(MOLMO_PREFIX)
    except Exception:
        return False  # treat unreadable files as fallback too


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", required=True,
                   help="Coordinator output dir containing *.jsonl and coordinator_state.json")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be done without making changes")
    args = p.parse_args()

    out_dir    = Path(args.output_dir)
    state_path = out_dir / "coordinator_state.json"

    with open(state_path) as f:
        state = json.load(f)

    shards = state["shards"]
    fallback_shards: list[str] = []

    for shard_id, info in shards.items():
        if info["status"] != "done":
            continue
        jsonl = out_dir / (Path(shard_id).stem + ".jsonl")
        if not jsonl.exists():
            continue
        if not is_molmo(jsonl):
            fallback_shards.append(shard_id)

    print(f"Found {len(fallback_shards)} fallback shards to re-queue:")
    for s in sorted(fallback_shards):
        print(f"  {s}")

    if not fallback_shards:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("\n[dry-run] No changes made.")
        return

    # Delete JSONL files and reset state to queued
    deleted = 0
    for shard_id in fallback_shards:
        jsonl = out_dir / (Path(shard_id).stem + ".jsonl")
        jsonl.unlink()
        deleted += 1
        state["shards"][shard_id]["status"] = "queued"
        state["shards"][shard_id]["worker"] = None
        state["shards"][shard_id].pop("started_at", None)
        state["shards"][shard_id].pop("finished_at", None)

    # Atomic write
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)

    print(f"\nDeleted {deleted} JSONL files, reset {deleted} shards to queued.")
    print("Restart the coordinator to pick up the changes.")


if __name__ == "__main__":
    main()
