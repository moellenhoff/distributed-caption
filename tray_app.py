#!/usr/bin/env python3
"""
Molmo@Work — Windows System Tray App
======================================
Shows worker status in the system tray and allows toggling via right-click menu.

Installed automatically by install_worker.ps1.

Manual start:
    python tray_app.py --coordinator http://10.0.0.1:5000 --worker-name win-4090
"""
from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw
import pystray

TASK_NAME     = "CaptionWorker"
LOG_PATH      = Path.home() / "caption-worker" / "logs" / "worker.log"
POLL_INTERVAL = 15    # seconds between status refreshes
OFFLINE_AFTER = 120   # seconds without heartbeat → offline


# ── Tray icons (colored circles) ─────────────────────────────────────────────

def _make_icon(color: tuple[int, int, int]) -> Image.Image:
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, 58, 58], fill=color)
    return img

ICON_ACTIVE  = _make_icon((30,  144, 255))   # blue  — working
ICON_IDLE    = _make_icon((160, 160, 160))   # grey  — waiting
ICON_OFFLINE = _make_icon((220,  50,  50))   # red   — stopped


# ── Worker control (Task Scheduler) ──────────────────────────────────────────

def _worker_running() -> bool:
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV"],
        capture_output=True, text=True,
    )
    return "Running" in r.stdout


def _start_worker() -> None:
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)


def _stop_worker() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)


# ── Coordinator polling ───────────────────────────────────────────────────────

def _queue_status(coordinator: str) -> str:
    try:
        r = requests.get(f"{coordinator}/status", timeout=4)
        d = r.json()
        total = d.get("total", 0)
        done  = d.get("shards", {}).get("done", 0)
        pct   = int(done / total * 100) if total else 0
        return f"Queue: {done}/{total} done ({pct}%)"
    except Exception:
        return "Queue: unreachable"


def _current_shard(coordinator: str, worker_name: str) -> str | None:
    try:
        r = requests.get(f"{coordinator}/status", timeout=4)
        info = r.json().get("workers", {}).get(worker_name, {})
        shard     = info.get("current_shard")
        last_seen = info.get("last_seen", 0)
        if shard and (time.time() - last_seen) < OFFLINE_AFTER:
            return Path(shard).name
    except Exception:
        pass
    return None


# ── Main tray app ─────────────────────────────────────────────────────────────

class TrayApp:

    def __init__(self, coordinator: str, worker_name: str) -> None:
        self.coordinator = coordinator
        self.worker_name = worker_name
        self._status_text = "Starting…"
        self._queue_text  = ""
        self._active      = _worker_running()

        self.icon = pystray.Icon(
            name  = "MolmoWorker",
            icon  = ICON_IDLE,
            title = "Molmo@Work",
            menu  = self._build_menu(),
        )

        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _: self._status_text, None, enabled=False),
            pystray.MenuItem(lambda _: self._queue_text,  None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: "Disable worker" if self._active else "Enable worker",
                self._toggle,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log file", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _toggle(self, icon, item) -> None:
        if self._active:
            _stop_worker()
            self._active = False
        else:
            _start_worker()
            self._active = True
        self._refresh()

    def _open_log(self, icon, item) -> None:
        import os
        os.startfile(str(LOG_PATH))   # opens in Notepad or default .log viewer

    def _quit(self, icon, item) -> None:
        icon.stop()

    # ── Status refresh ────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._active = _worker_running()

        if not self._active:
            self.icon.icon  = ICON_OFFLINE
            self.icon.title = "Molmo@Work — stopped"
            self._status_text = "● Stopped"
            self._queue_text  = ""
            return

        shard = _current_shard(self.coordinator, self.worker_name)
        queue = _queue_status(self.coordinator)
        self._queue_text = queue

        if shard:
            self.icon.icon    = ICON_ACTIVE
            self.icon.title   = f"Molmo@Work — {shard}"
            self._status_text = f"● Working — {shard}"
        else:
            self.icon.icon    = ICON_IDLE
            self.icon.title   = "Molmo@Work — waiting for job"
            self._status_text = "● Waiting for job…"

        self.icon.update_menu()

    def _poll_loop(self) -> None:
        while True:
            try:
                self._refresh()
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    def run(self) -> None:
        self.icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Molmo@Work Windows Tray App")
    p.add_argument("--coordinator", required=True)
    p.add_argument("--worker-name", default=None)
    args = p.parse_args()

    import socket
    worker_name = args.worker_name or socket.gethostname()

    app = TrayApp(coordinator=args.coordinator, worker_name=worker_name)
    app.run()


if __name__ == "__main__":
    main()
