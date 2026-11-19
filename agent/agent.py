#!/usr/bin/env python3
"""xrnmoni agent — runs on each monitored machine (your own PC / laptop).

Tracks:
  * boot time (reported once at start)
  * foreground window intervals  -> {app, title, duration}
  * idle / AFK intervals
  * browser page intervals forwarded from the browser extension

Every event is written to an on-disk spool (SQLite) as soon as it is made, and
deleted only once the server has taken it, so restarts, crashes and server
outages lose nothing that was already recorded.

Usage:
    pip install -r requirements.txt
    cp config.example.toml config.toml   # then edit it
    python agent.py --config config.toml
"""
import argparse
import hashlib
import os
import sys
import time
import json
import re
import signal
import socket
import sqlite3
import threading
import uuid
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


def log(msg):
    print(msg, flush=True)


_ID = re.compile(r"[0-9a-f]{32}")


def new_id():
    """An event's id: the server keeps the first event with a given id and ignores
    resends, so a batch delivered twice (answer lost, crash before the delete) isn't
    counted twice."""
    return uuid.uuid4().hex


def derived_id(*parts):
    """The same id for the same parts, shaped like a UUID4 hex."""
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).digest()
    return uuid.UUID(bytes=digest[:16], version=4).hex


def valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def boot_event(device, boot_time):
    # every start reports the same boot; one id per boot keeps it to one row
    return {"id": derived_id("boot", device, int(boot_time)), "type": "boot",
            "ts_start": boot_time}


def default_spool_path(platform=sys.platform, env=os.environ, home=os.path.expanduser("~")):
    """Where the outbox lives when the config doesn't say: the per-user state folder."""
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return os.path.join(base, "xrnmoni", "spool.sqlite")


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
        self.spool_path = os.path.expanduser(c.get("spool_path") or default_spool_path())
        self.spool_max_days = float(c.get("spool_max_days", 30))
        self.spool_max_rows = int(c.get("spool_max_rows", 200000))


class Spool:
    """The outbox: a SQLite file holding every event the server hasn't taken yet.

    Events go in the moment they're made and come out only after the server answered
    2xx for them, so a crash, a reboot or a week without the server loses nothing.
    The cap (age and row count) drops the oldest first, so a server that is gone for
    good can't fill the disk."""

    def __init__(self, path, max_rows=200000, max_days=30):
        self.path = path
        self.max_rows = max_rows
        self.max_age = max_days * 86400
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL)")

    def add(self, events):
        rows = [(float(e.get("ts_end") or e.get("ts_start") or time.time()), json.dumps(e))
                for e in events]
        with self._lock:
            self._db.executemany("INSERT INTO outbox (ts, event) VALUES (?, ?)", rows)

    def peek(self, n):
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, event FROM outbox ORDER BY seq LIMIT ?", (n,)).fetchall()
        return [(seq, json.loads(event)) for seq, event in rows]

    def remove(self, seqs):
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE seq = ?", [(s,) for s in seqs])

    def trim(self, now=None):
        """Apply the cap; returns how many events were dropped."""
        now = time.time() if now is None else now
        with self._lock:
            dropped = self._db.execute(
                "DELETE FROM outbox WHERE ts < ?", (now - self.max_age,)).rowcount
            over = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] - self.max_rows
            if over > 0:
                dropped += self._db.execute(
                    "DELETE FROM outbox WHERE seq IN"
                    " (SELECT seq FROM outbox ORDER BY seq LIMIT ?)", (over,)).rowcount
        return dropped

    def __len__(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()


# Answers that say the request, not the events, is wrong (token, address, rate limit):
# keep everything and try again later instead of dropping good data.
KEEP_STATUSES = {401, 403, 404, 405, 407, 408, 409, 425, 429}


class Sender(threading.Thread):
    """Sends the spool to the server in batches, oldest first.

    2xx: the batch is deleted (events the server lists as rejected are logged).
    Other 4xx: each event is retried alone once; the ones still refused are dropped and
    logged, so one bad event can't hold up everything after it. 5xx, network errors and
    the statuses in KEEP_STATUSES keep the events and back off exponentially."""

    BATCH = 250
    MAX_DELAY = 300.0

    def __init__(self, cfg, spool):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.spool = spool
        self.url = f"{cfg.server_url.rstrip('/')}/api/events"
        self.headers = {"Authorization": f"Bearer {cfg.token}"}
        self.session = requests.Session()
        self.timeout = 10
        self.wake = threading.Event()
        self.stopping = False

    def put(self, event):
        self.put_many([event])

    def put_many(self, events):
        for e in events:
            if not valid_id(e.get("id")):
                e["id"] = new_id()
        if events:
            self.spool.add(events)

    def next_delay(self, failures):
        return min(self.cfg.flush_interval * 2 ** (failures - 1), self.MAX_DELAY)

    def _post(self, events):
        """POST events; returns "ok", "keep" or "refused"."""
        body = {"device": self.cfg.device, "os": OS_NAME, "events": events}
        try:
            r = self.session.post(self.url, json=body, headers=self.headers,
                                  timeout=self.timeout)
        except requests.RequestException as e:
            log(f"[sender] server unreachable, {len(self.spool)} events kept ({e})")
            return "keep"
        if 200 <= r.status_code < 300:
            try:
                reply = r.json()
            except ValueError:
                reply = None
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                # a proxy or captive portal answered, not the xrnmoni server
                log(f"[sender] unexpected answer from {self.url}: {r.text[:120]!r}; events kept")
                return "keep"
            for rej in reply.get("rejected") or []:
                log(f"[sender] server rejected an event, dropped: {rej}")
            return "ok"
        if r.status_code >= 500 or r.status_code in KEEP_STATUSES:
            log(f"[sender] server said {r.status_code}: {r.text[:120]}; events kept")
            return "keep"
        return "refused"

    def send_once(self):
        """Send one batch. Returns "empty", "sent" or "retry" (back off)."""
        batch = self.spool.peek(self.BATCH)
        if not batch:
            return "empty"
        result = self._post([e for _, e in batch])
        if result == "ok":
            self.spool.remove([seq for seq, _ in batch])
            return "sent"
        if result == "keep":
            return "retry"
        for seq, event in batch:
            result = self._post([event]) if len(batch) > 1 else "refused"
            if result == "keep":
                return "retry"
            if result == "refused":
                log(f"[sender] server refused an event, dropped: {json.dumps(event)[:200]}")
            self.spool.remove([seq])
        return "sent"

    def run(self):
        failures = 0
        while not self.stopping:
            try:
                dropped = self.spool.trim()
                if dropped:
                    log(f"[sender] spool over its cap, dropped the {dropped} oldest events")
                result = self.send_once()
            except Exception as e:  # never let the sender thread die quietly
                log(f"[sender] error: {e!r}")
                result = "retry"
            if result == "retry":
                failures += 1
                delay = self.next_delay(failures)
            else:
                failures = 0
                delay = 0 if result == "sent" else self.cfg.flush_interval
            if delay:
                self.wake.wait(delay)
                self.wake.clear()

    def stop(self, timeout=5.0):
        """Try once more to deliver what's left (it stays in the spool if that fails)."""
        self.stopping = True
        self.wake.set()
        if self.is_alive():
            self.join(timeout)
        if self.is_alive():  # still waiting on the server; it'll get the rest next start
            return
        self.timeout = 3
        deadline = time.time() + timeout
        while time.time() < deadline and self.send_once() == "sent":
            pass


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
                    "id": it.get("id"),
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


CHUNK = 60.0  # the longest interval the agent sends


def pieces(start, end):
    """[start, end] cut into consecutive pieces of at most CHUNK seconds."""
    out = []
    while start < end:
        stop = min(end, start + CHUNK)
        out.append((start, stop))
        start = stop
    return out


def elapsed_clock():
    """Seconds that keep counting while the machine is suspended. Linux's monotonic
    clock stops during suspend, CLOCK_BOOTTIME doesn't; elsewhere use wall time."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.time()


class Tracker:
    """Samples the foreground window and idle time and turns them into intervals.

    The open interval is sent in closed chunks of CHUNK seconds as it goes, so an
    hour in one window shows up minute by minute, and a crash loses at most the
    current minute. close() ends it at "now" (on shutdown).

    If the loop wakes to find more wall time gone than it slept (poll_interval + 10 s),
    the machine was suspended or the agent stopped: the open interval ends where it was
    last seen and the gap is recorded as "sleep", instead of counting it as app time.
    A jump of the wall clock alone (the time was set) ends the interval the same way
    without inventing a sleep."""

    def __init__(self, cfg, put_many, backend, now=time.time, clock=elapsed_clock):
        self.cfg = cfg
        self.put_many = put_many
        self.backend = backend
        self.now = now
        self.clock = clock
        # the open interval; "start" is where its unsent part begins
        self.cur = None  # {"type", "app", "title", "start"}
        self.last = None  # (wall, elapsed) at the previous tick
        self.wake = threading.Event()

    def _emit(self, cur, end):
        events = []
        for a, b in pieces(cur["start"], end):
            e = {"id": new_id(), "type": cur["type"], "ts_start": a, "ts_end": b,
                 "duration": b - a}
            if cur["type"] == "app":
                e["app"], e["title"] = cur["app"], cur["title"]
            events.append(e)
        if events:
            self.put_many(events)

    def _close(self, end):
        cur, self.cur = self.cur, None
        if cur and end > cur["start"]:
            self._emit(cur, end)

    def _open(self, kind, start, app=None, title=None):
        self.cur = {"type": kind, "app": app, "title": title, "start": start}

    def _check_gap(self, now, elapsed):
        if self.last is None:
            return
        last_wall, last_elapsed = self.last
        limit = self.cfg.poll_interval + 10
        if now - last_wall > limit and elapsed - last_elapsed > limit:
            self._close(last_wall)
            self._emit({"type": "sleep", "start": last_wall}, now)
        elif now - last_wall > limit or now < last_wall:
            self._close(last_wall)

    def tick(self):
        now = self.now()
        elapsed = self.clock()
        self._check_gap(now, elapsed)
        self.last = (now, elapsed)
        cur = self.cur
        try:
            idle = self.backend.get_idle_seconds()
        except Exception:
            idle = 0.0

        if idle >= self.cfg.afk_threshold:
            # away: the app interval ended when input stopped
            if not cur or cur["type"] != "afk":
                since = now - idle
                self._close(max(since, cur["start"]) if cur else since)
                self._open("afk", since)
        else:
            if cur and cur["type"] == "afk":
                self._close(now)
                cur = None
            try:
                app, title = self.backend.get_active_window()
            except Exception as e:
                app, title = None, None
                log(f"[tracker] active-window error: {e}")
            if app is None:
                self._close(now)
            elif cur is None or cur["app"] != app or cur["title"] != title:
                self._close(now)
                self._open("app", now, app, title)

        cur = self.cur
        if cur and now - cur["start"] >= CHUNK:
            end = cur["start"] + (now - cur["start"]) // CHUNK * CHUNK
            self._emit(cur, end)
            cur["start"] = end

    def close(self):
        self._close(self.now())

    def run(self, stop):
        while not stop.is_set():
            self.wake.clear()
            try:
                self.tick()
            except Exception as e:
                log(f"[tracker] error: {e!r}")
            self.wake.wait(self.cfg.poll_interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.toml"))
    args = ap.parse_args()
    cfg = Config(args.config)

    sender = Sender(cfg, Spool(cfg.spool_path, cfg.spool_max_rows, cfg.spool_max_days))
    sender.start()

    # report real boot time once
    sender.put(boot_event(cfg.device, psutil.boot_time()))

    bridge = BrowserBridge(cfg, sender)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()

    tracker = Tracker(cfg, sender.put_many, backend)
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
        tracker.wake.set()

    # systemd stops with SIGTERM, a terminal with SIGINT/SIGHUP: in every case close the
    # open interval at "now" and put it in the spool before exiting
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    print(
        f"[xrnmoni] device={cfg.device} os={OS_NAME} backend={backend.NAME} "
        f"-> {cfg.server_url} (bridge :{bridge.server_address[1]})",
        flush=True,
    )
    try:
        tracker.run(stop)
    finally:
        tracker.close()
        sender.put({"type": "shutdown", "ts_start": time.time()})
        sender.stop()


if __name__ == "__main__":
    main()
