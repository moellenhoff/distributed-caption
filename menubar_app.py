#!/usr/bin/env python3
"""
Molmo@Work — macOS Menu Bar App
=================================
Zeigt den Worker-Status in der Menüleiste und erlaubt das
Aktivieren/Deaktivieren per Klick — keine Terminalkenntnisse nötig.

Installation:
    Wird automatisch durch install_worker.sh eingerichtet.

Manuell starten:
    python menubar_app.py --coordinator http://10.0.0.1:5000 --worker-name mac-studio-1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import rumps

PLIST_LABEL    = "com.pd.caption-worker"
PLIST_PATH     = Path.home() / "Library/LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_PATH       = Path.home() / "caption-worker/logs/worker.log"
POLL_INTERVAL  = 15   # Sekunden zwischen Status-Updates
OFFLINE_AFTER  = 120  # Sekunden ohne Heartbeat → offline

# Icons
ICON_ACTIVE  = "🔵"
ICON_IDLE    = "⚪️"
ICON_OFFLINE = "🔴"


class MolmoWorkerApp(rumps.App):

    def __init__(self, coordinator: str, worker_name: str) -> None:
        super().__init__(ICON_IDLE, quit_button=None)
        self.coordinator  = coordinator
        self.worker_name  = worker_name
        self._active      = self._worker_running()

        # Menu items
        self._status_item = rumps.MenuItem("Starte …")
        self._status_item.set_callback(None)   # nicht klickbar

        self._toggle_item = rumps.MenuItem(
            "Deaktivieren" if self._active else "Aktivieren",
            callback=self.toggle,
        )

        self._coordinator_item = rumps.MenuItem(f"Coordinator: {coordinator}")
        self._coordinator_item.set_callback(None)

        self._queue_item = rumps.MenuItem("")
        self._queue_item.set_callback(None)

        self.menu = [
            self._status_item,
            None,
            self._toggle_item,
            None,
            self._coordinator_item,
            self._queue_item,
            None,
            rumps.MenuItem("Beenden", callback=self.quit_app),
        ]

        # Background update thread
        t = threading.Thread(target=self._update_loop, daemon=True)
        t.start()

    # ── Worker control ────────────────────────────────────────────────────────

    def _worker_running(self) -> bool:
        """Check if the LaunchAgent is loaded (worker active)."""
        result = subprocess.run(
            ["launchctl", "list", PLIST_LABEL],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def _start_worker(self) -> None:
        subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True)

    def _stop_worker(self) -> None:
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)

    def toggle(self, _) -> None:
        if self._active:
            self._stop_worker()
            self._active = False
            rumps.notification(
                "Molmo@Work", "Worker deaktiviert",
                "Dieser Rechner nimmt keine neuen Jobs an.",
                sound=False,
            )
        else:
            self._start_worker()
            self._active = True
            rumps.notification(
                "Molmo@Work", "Worker aktiviert",
                "Dieser Rechner holt sich jetzt Jobs vom Coordinator.",
                sound=False,
            )
        self._refresh_ui()

    def quit_app(self, _) -> None:
        rumps.quit_application()

    # ── Status polling ────────────────────────────────────────────────────────

    def _last_log_line(self) -> str:
        """Read the last meaningful line from the worker log."""
        if not LOG_PATH.exists():
            return ""
        try:
            lines = LOG_PATH.read_text(errors="replace").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line:
                    # Strip timestamp prefix if present
                    parts = line.split(" ", 2)
                    return parts[-1] if len(parts) >= 3 else line
        except Exception:
            pass
        return ""

    def _queue_status(self) -> str:
        """Fetch queue summary from coordinator."""
        try:
            r = requests.get(f"{self.coordinator}/status", timeout=4)
            d = r.json()
            s = d.get("shards", {})
            total = d.get("total", 0)
            done  = s.get("done", 0)
            pct   = int(done / total * 100) if total else 0
            return f"Queue: {done}/{total} fertig ({pct}%)"
        except Exception:
            return "Queue: nicht erreichbar"

    def _current_shard(self) -> str | None:
        """Ask coordinator what this worker is currently doing."""
        try:
            r = requests.get(f"{self.coordinator}/status", timeout=4)
            workers = r.json().get("workers", {})
            info = workers.get(self.worker_name, {})
            shard = info.get("current_shard")
            last_seen = info.get("last_seen", 0)
            if shard and (time.time() - last_seen) < OFFLINE_AFTER:
                return Path(shard).name
        except Exception:
            pass
        return None

    def _refresh_ui(self) -> None:
        self._active = self._worker_running()
        self._toggle_item.title = "Deaktivieren" if self._active else "Aktivieren"

        if not self._active:
            self.title = ICON_OFFLINE
            self._status_item.title = "● Inaktiv — pausiert"
            self._queue_item.title  = ""
            return

        shard = self._current_shard()
        queue = self._queue_status()

        if shard:
            self.title = ICON_ACTIVE
            self._status_item.title = f"● Aktiv — {shard}"
        else:
            self.title = ICON_IDLE
            self._status_item.title = "● Wartet auf Job …"

        self._queue_item.title = queue

    def _update_loop(self) -> None:
        while True:
            try:
                self._refresh_ui()
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Molmo@Work Menu Bar App")
    p.add_argument("--coordinator", required=True)
    p.add_argument("--worker-name", default=None)
    args = p.parse_args()

    import socket
    worker_name = args.worker_name or socket.gethostname()

    app = MolmoWorkerApp(coordinator=args.coordinator, worker_name=worker_name)
    app.run()


if __name__ == "__main__":
    main()
