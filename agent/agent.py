#!/usr/bin/env python3
"""xrnmoni agent — runs on each monitored machine (your own PC / laptop).

Tracks:
  * boot time (reported once at start)
  * foreground window intervals  -> {app, title, duration}
  * idle / AFK intervals
  * browser page intervals forwarded from the browser extension

Sends everything in small batches to the central server.

Usage:
    pip install -r requirements.txt
    cp config.example.toml config.toml   # then edit it
    python agent.py --config config.toml
"""
import argparse
import os
import sys
import time
import json
import re
import socket
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import requests

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

# ---- platform backend ------------------------------------------------------
if sys.platform.startswith("win"):
    import platform_win as backend
    OS_NAME = "windows"
elif sys.platform.startswith("linux"):
    import platform_linux as backend
    OS_NAME = "linux"
else:
    raise SystemExit(f"unsupported platform: {sys.platform}")


class Config:
    def __init__(self, path):
        with open(path, "rb") as f:
            c = tomllib.load(f)
        self.server_url = c["server_url"].rstrip("/")
        self.token = c["token"]
        self.device = c.get("device") or socket.gethostname()
        self.poll_interval = float(c.get("poll_interval", 2.0))
        self.flush_interval = float(c.get("flush_interval", 10.0))
        self.afk_threshold = float(c.get("afk_threshold", 60.0))
        self.bridge_port = int(c.get("bridge_port", 8129))


class Sender(threading.Thread):
    """Owns the outgoing queue; batches and POSTs to the server with retry."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = queue.Queue()
        self._pending = []  # events not yet acknowledged by the server

    def put(self, event):
        self.q.put(event)

    def run(self):
        session = requests.Session()
        url = f"{self.cfg.server_url}/api/events"
        headers = {"Authorization": f"Bearer {self.cfg.token}"}
        while True:
            try:
                self._pending.append(self.q.get(timeout=self.cfg.flush_interval))
            except queue.Empty:
                pass
            # drain whatever else is queued
            while True:
                try:
                    self._pending.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if not self._pending:
                continue
            body = {"device": self.cfg.device, "os": OS_NAME, "events": self._pending}
            try:
                r = session.post(url, json=body, headers=headers, timeout=10)
                if r.status_code == 200:
                    self._pending = []
                else:
                    print(f"[sender] server said {r.status_code}: {r.text[:120]}", flush=True)
            except requests.RequestException as e:
                print(f"[sender] offline, buffering {len(self._pending)} events ({e})", flush=True)
                # cap buffer so we don't grow forever while the server is down
                if len(self._pending) > 10000:
                    self._pending = self._pending[-10000:]


class BrowserBridge(ThreadingHTTPServer):
    """Tiny localhost HTTP server the browser extension posts page intervals to.

    The extension never sees the server URL or token — it only talks to
    127.0.0.1, and we tag + forward. Bound to loopback only.
    """

    def __init__(self, cfg, sender):
        self.cfg = cfg
        self.sender = sender
        super().__init__(("127.0.0.1", cfg.bridge_port), _BridgeHandler)


# Only our own browser extension may post. Browsers always send the real Origin, and a web
# page can't claim an extension's, so without this any website you open could feed the
# agent made-up activity (and, before the dashboard escaped it, script for the dashboard).
EXTENSION_ORIGIN = re.compile(r"^(chrome-extension|moz-extension)://[A-Za-z0-9._-]+$")
MAX_BODY = 256 * 1024


class _BridgeHandler(BaseHTTPRequestHandler):
    def _origin_ok(self):
        return bool(EXTENSION_ORIGIN.match(self.headers.get("Origin", "")))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _refuse(self, code):
        self.send_response(code)
        self.end_headers()

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._refuse(403)
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self._origin_ok():
            return self._refuse(403)
        try:
            if int(self.headers.get("Content-Length", 0)) > MAX_BODY:
                return self._refuse(413)
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        # one interval, or a list of them
        items = data if isinstance(data, list) else [data]
        for it in items:
            self.server.sender.put(
                {
                    "type": "browser",
                    "url": it.get("url"),
                    "title": it.get("title"),
                    "domain": it.get("domain"),
                    "ts_start": it.get("ts_start"),
                    "ts_end": it.get("ts_end"),
                    "duration": it.get("duration"),
                }
            )
        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):  # silence per-request logging
        pass


def run_tracker(cfg, sender):
    """Main sampling loop: foreground window + idle."""
    cur = None  # {"app", "title", "start"}
    afk_start = None

    def close_app_interval(end):
        nonlocal cur
        if cur and end > cur["start"]:
            sender.put(
                {
                    "type": "app",
                    "app": cur["app"],
                    "title": cur["title"],
                    "ts_start": cur["start"],
                    "ts_end": end,
                    "duration": end - cur["start"],
                }
            )
        cur = None

    while True:
        now = time.time()
        try:
            idle = backend.get_idle_seconds()
        except Exception:
            idle = 0.0

        if idle >= cfg.afk_threshold:
            # user is away: close any open app interval, open/extend an afk interval
            if cur:
                close_app_interval(now - idle)  # app ended when input stopped
            if afk_start is None:
                afk_start = now - idle
            time.sleep(cfg.poll_interval)
            continue

        # user is active again -> flush afk interval
        if afk_start is not None:
            sender.put(
                {
                    "type": "afk",
                    "ts_start": afk_start,
                    "ts_end": now,
                    "duration": now - afk_start,
                }
            )
            afk_start = None

        try:
            app, title = backend.get_active_window()
        except Exception as e:
            app, title = None, None
            print(f"[tracker] active-window error: {e}", flush=True)

        if app is None:
            close_app_interval(now)
        elif cur is None or cur["app"] != app or cur["title"] != title:
            close_app_interval(now)
            cur = {"app": app, "title": title, "start": now}

        time.sleep(cfg.poll_interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.toml"))
    args = ap.parse_args()
    cfg = Config(args.config)

    sender = Sender(cfg)
    sender.start()

    # report real boot time once
    sender.put({"type": "boot", "ts_start": psutil.boot_time()})

    bridge = BrowserBridge(cfg, sender)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()

    print(
        f"[xrnmoni] device={cfg.device} os={OS_NAME} backend={backend.NAME} "
        f"-> {cfg.server_url} (bridge :{cfg.bridge_port})",
        flush=True,
    )
    try:
        run_tracker(cfg, sender)
    except KeyboardInterrupt:
        sender.put({"type": "shutdown", "ts_start": time.time()})
        time.sleep(cfg.flush_interval + 2)  # give the sender a chance to flush


if __name__ == "__main__":
    main()
