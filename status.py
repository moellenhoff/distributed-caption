#!/usr/bin/env python3
"""
Caption queue status display.

Usage:
    python status.py --coordinator http://10.0.0.1:5000
    python status.py --coordinator http://10.0.0.1:5000 --watch   # refresh every 10s
"""
from __future__ import annotations

import argparse
import time

import requests

OFFLINE_AFTER = 120   # seconds without heartbeat → show as offline


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _bar(done: int, total: int, width: int = 30) -> str:
    if total == 0:
        return "[" + " " * width + "]"
    filled = int(done / total * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def show(coordinator: str) -> None:
    try:
        r = requests.get(f"{coordinator}/status", timeout=5)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ✗ Coordinator unreachable: {e}")
        return

    shards  = data.get("shards", {})
    workers = data.get("workers", {})
    total   = data.get("total", 0)
    done    = shards.get("done", 0)
    active  = shards.get("in_progress", 0)
    queued  = shards.get("queued", 0)
    pct     = done / total * 100 if total else 0

    print(f"\n{'─' * 48}")
    print(f"  Caption Queue  —  {coordinator}")
    print(f"{'─' * 48}")
    print(f"  {_bar(done, total)}  {pct:.1f}%")
    print(f"  Done:        {done:>5} / {total}")
    print(f"  In progress: {active:>5}")
    print(f"  Queued:      {queued:>5}")
    print()

    if not workers:
        print("  No workers registered yet.")
    else:
        print(f"  {'Worker':<20}  {'Status':<8}  {'Current shard':<30}  Last seen")
        print(f"  {'─'*20}  {'─'*8}  {'─'*30}  {'─'*12}")
        now = time.time()
        for name, info in sorted(workers.items()):
            last  = info.get("last_seen") or 0
            shard = info.get("current_shard")
            shard_name = shard.split("/")[-1] if shard else "—"
            online = (now - last) < OFFLINE_AFTER
            status = "✓ online" if online else "✗ offline"
            print(f"  {name:<20}  {status:<8}  {shard_name:<30}  {_ago(last)}")

    print(f"{'─' * 48}\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--coordinator", required=True)
    p.add_argument("--watch", action="store_true",
                   help="Refresh every 10 seconds (Ctrl+C to stop)")
    p.add_argument("--interval", type=int, default=10)
    args = p.parse_args()

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")   # clear screen
                show(args.coordinator)
                print(f"  Refreshing every {args.interval}s — Ctrl+C to stop")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        show(args.coordinator)


if __name__ == "__main__":
    main()
