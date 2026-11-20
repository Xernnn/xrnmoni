#!/usr/bin/env python3
"""xrnmoni agent — runs on each monitored machine (your own PC / laptop).

Tracks:
  * boot time (reported once at start)
  * foreground window intervals  -> {app, title, duration}
  * idle intervals (no input for afk_threshold seconds)
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
import datetime
import hashlib
import hmac
import math
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
from urllib.parse import urlsplit, urlunsplit

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


BOOT_ROUND_S = 10


def boot_event(device, boot_time, boot_id=None):
    """The boot the agent runs in: every start reports it, and one id per boot keeps it to
    one row. Linux names each boot (boot_id). Elsewhere the boot time stands for it, to the
    nearest BOOT_ROUND_S: it is the clock minus the uptime, which moves a little from one
    start to the next."""
    key = boot_id or round(boot_time / BOOT_ROUND_S)
    return {"id": derived_id("boot", device, key), "type": "boot", "ts_start": boot_time}


DEVICE_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}")


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
        if not DEVICE_NAME.fullmatch(self.device):
            # the server refuses the whole batch for such a name; retried one event at a
            # time, that would drop every event
            raise SystemExit(f"xrnmoni: device {self.device!r} must be 1-64 characters "
                             "with no control characters")
        self.poll_interval = float(c.get("poll_interval", 2.0))
        self.flush_interval = float(c.get("flush_interval", 10.0))
        self.afk_threshold = float(c.get("afk_threshold", 60.0))
        self.bridge_port = int(c.get("bridge_port", 8129))
        self.spool_path = os.path.expanduser(c.get("spool_path") or default_spool_path())
        self.spool_max_days = float(c.get("spool_max_days", 30))
        self.spool_max_rows = int(c.get("spool_max_rows", 200000))
        self.bridge_origins = [str(o) for o in c.get("bridge_origins", [])]
        self.bridge_secret = str(c.get("bridge_secret", ""))
        self.keep_url_query = bool(c.get("keep_url_query", False))


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

    def add(self, events, now=None):
        # the age cap counts from here: how long an event has waited, not how old it is
        now = time.time() if now is None else now
        rows = [(now, json.dumps(e)) for e in events]
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
        """Apply the cap (spool_max_days unsent, spool_max_rows); returns how many events
        were dropped."""
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

    A post is taken only from an origin listed in bridge_origins that sends
    bridge_secret in X-Xrnmoni-Secret. Page time that falls in an idle, locked or
    sleep interval of the agent is cut out (the tracker passed in knows them).
    """

    def __init__(self, cfg, sender, tracker=None):
        self.cfg = cfg
        self.sender = sender
        self.tracker = tracker
        self.origins = set(getattr(cfg, "bridge_origins", None) or [])
        self.secret = getattr(cfg, "bridge_secret", "") or ""
        self.keep_query = bool(getattr(cfg, "keep_url_query", False))
        self.logged = set()
        super().__init__(("127.0.0.1", cfg.bridge_port), _BridgeHandler)

    def log_once(self, key, msg):
        if key not in self.logged and len(self.logged) < 50:
            self.logged.add(key)
            log(msg)


# Only our own browser extension may post. Browsers always send the real Origin, and a web
# page can't claim an extension's, so without this any website you open could feed the
# agent made-up activity (and, before the dashboard escaped it, script for the dashboard).
# The origin must also be one listed in bridge_origins, so another extension can't post,
# and the shared secret keeps out local programs, which can send any Origin they like.
EXTENSION_ORIGIN = re.compile(r"^(chrome-extension|moz-extension)://[A-Za-z0-9._-]+$")
SECRET_HEADER = "X-Xrnmoni-Secret"
MAX_BODY = 256 * 1024
# A refused post's body is read and dropped up to this much before answering: a socket closed
# with unread data is reset on Windows, and the sender may lose the answer.
DRAIN_LIMIT = 4 * 1024 * 1024
MAX_ITEMS = 100
MAX_SPAN = 86400.0
EARLIEST = 946684800.0  # 2000-01-01


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean_url(url, keep_query=False):
    """Drop user:password@ always, and the ?query and #fragment unless keep_query: they
    carry search terms, session tokens and message ids, not which site it was."""
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    if keep_query:
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def browser_item(it, now, keep_query=False):
    """Check one posted interval; returns the event, or raises ValueError."""
    if not isinstance(it, dict):
        raise ValueError("an interval must be an object")
    start, end = it.get("ts_start"), it.get("ts_end")
    if not (_number(start) and _number(end)):
        raise ValueError("ts_start and ts_end must be numbers")
    if not EARLIEST <= start <= end <= now + 300 or end - start > MAX_SPAN:
        raise ValueError("ts_start/ts_end out of range")
    url, title = it.get("url"), it.get("title")
    if url is not None and (not isinstance(url, str) or len(url) > 8192):
        raise ValueError("url must be a string")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")
    domain = None
    if url:
        url = clean_url(url, keep_query)
        domain = (urlsplit(url).hostname or "").lower() or None
    return {"id": it.get("id") if valid_id(it.get("id")) else new_id(), "type": "browser",
            "url": url, "title": (title or "")[:1000], "domain": domain,
            "ts_start": float(start), "ts_end": float(end)}


def subtract(start, end, periods):
    """[start, end] minus the (from, to) periods, as a list of (a, b)."""
    out = [(start, end)]
    for p0, p1 in periods:
        out = [piece for a, b in out
               for piece in ((a, min(b, p0)), (max(a, p1), b)) if piece[1] > piece[0]]
    return out


class _BridgeHandler(BaseHTTPRequestHandler):
    def _origin(self):
        return self.headers.get("Origin", "")

    def _origin_ok(self):
        origin = self._origin()
        if not EXTENSION_ORIGIN.match(origin):
            return False
        if origin not in self.server.origins:
            self.server.log_once(origin, f"[bridge] refused {origin}: if that is the xrnmoni "
                                         "extension, add it to bridge_origins in the config")
            return False
        return True

    def _secret_ok(self):
        secret = self.server.secret
        given = self.headers.get(SECRET_HEADER, "")
        if secret and hmac.compare_digest(given.encode(), secret.encode()):
            return True
        self.server.log_once(("secret", self._origin()),
                             f"[bridge] {self._origin()} sent no or the wrong {SECRET_HEADER};"
                             " set the extension's secret to bridge_secret from the config")
        return False

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self._origin())
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, " + SECRET_HEADER)
        self.send_header("Vary", "Origin")

    def _drain(self, length):
        if not 0 < length <= DRAIN_LIMIT:
            return
        try:
            self.connection.settimeout(2)
            while length > 0:
                chunk = self.rfile.read(min(length, 65536))
                if not chunk:
                    break
                length -= len(chunk)
        except OSError:
            pass

    def _refuse(self, code, cors=False, msg=None):
        self.send_response(code)
        if cors:
            self._cors()
        body = json.dumps({"ok": False, "error": msg}).encode() if msg else b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if self.path != "/browser":
            return self._refuse(404)
        if not self._origin_ok():
            return self._refuse(403)
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/browser":
            return self._refuse(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._refuse(400)
        if length > MAX_BODY:
            self._drain(length)
            return self._refuse(413)
        if not self._origin_ok() or not self._secret_ok():
            self._drain(length)
            return self._refuse(403)
        try:
            data = json.loads(self.rfile.read(length) or b"null",
                              parse_constant=lambda c: float("nan"))
            # one interval, or a list of them
            items = data if isinstance(data, list) else [data]
            if not items or len(items) > MAX_ITEMS:
                raise ValueError(f"send 1 to {MAX_ITEMS} intervals")
            now = time.time()
            events = [browser_item(it, now, self.server.keep_query) for it in items]
        except (ValueError, TypeError, RecursionError) as e:
            return self._refuse(400, cors=True, msg=str(e)[:200])
        n = 0
        tracker = self.server.tracker
        for e in events:
            away = tracker.away_periods(e["ts_start"]) if tracker else []
            kept = subtract(e["ts_start"], e["ts_end"], away)
            parts = [p for a, b in kept for p in pieces(a, b)]
            for i, (a, b) in enumerate(parts):
                piece = dict(e, ts_start=a, ts_end=b, duration=b - a)
                if parts != [(e["ts_start"], e["ts_end"])]:
                    piece["id"] = derived_id(e["id"], i)
                self.server.sender.put(piece)
                n += 1
        body = json.dumps({"ok": True, "accepted": n}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence per-request logging
        pass


CHUNK = 60.0  # the longest interval the agent sends
AWAY = ("idle", "locked", "sleep")  # no app or page time is counted in these


def next_midnight(ts):
    """The first local midnight after ts (mktime settles DST)."""
    d = datetime.date.fromtimestamp(ts) + datetime.timedelta(days=1)
    return time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1))


def pieces(start, end):
    """[start, end] cut into consecutive pieces of at most CHUNK seconds, and at every
    local midnight, so each piece belongs to one day and day totals add up."""
    out = []
    while start < end:
        stop = min(end, start + CHUNK, next_midnight(start))
        out.append((start, stop))
        start = stop
    return out


def elapsed_clock(platform=backend):
    """Seconds that keep counting while the machine is suspended and stay put when the
    clock is set. Windows: GetTickCount64 (the backend's elapsed_seconds). Linux:
    CLOCK_BOOTTIME (its monotonic clock stops during suspend). Elsewhere wall time, where
    setting the clock forward looks like a sleep."""
    ticks = getattr(platform, "elapsed_seconds", None)
    if ticks:
        return ticks()
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.time()


class Tracker:
    """Samples the foreground window, idle time, lock and sleep and turns them into
    intervals: app, idle, locked, sleep.

    The open interval is sent in closed chunks of CHUNK seconds as it goes, so an
    hour in one window shows up minute by minute, and a crash loses at most the
    current minute. close() ends it at "now" (on shutdown).

    If the loop wakes to find more wall time gone than it slept (poll_interval + 10 s),
    the machine was suspended or the agent stopped: the open interval ends where it was
    last seen and the gap is recorded as "sleep", instead of counting it as app time.
    A jump of the wall clock alone (the time was set) ends the interval the same way
    without inventing a sleep."""

    def __init__(self, cfg, put_many, backend, now=time.time, clock=None):
        self.cfg = cfg
        self.put_many = put_many
        self.backend = backend
        self.now = now
        self.clock = clock or (lambda: elapsed_clock(backend))
        # the open interval; "start" is where its unsent part begins
        self.cur = None  # {"type", "app", "title", "start"}
        self.last = None  # (wall, elapsed) at the previous tick
        self.wake = threading.Event()
        self._away = []   # closed (start, end) idle/locked/sleep periods, last day
        self._lock = threading.Lock()

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
        with self._lock:
            cur, self.cur = self.cur, None
            if cur and cur["type"] in AWAY and end > cur["since"]:
                self._away = [p for p in self._away if p[1] > end - 86400]
                self._away.append((cur["since"], end))
        if cur and end > cur["start"]:
            self._emit(cur, end)

    def _open(self, kind, start, app=None, title=None):
        with self._lock:
            self.cur = {"type": kind, "app": app, "title": title, "start": start,
                        "since": start}

    def away_periods(self, after):
        """Idle, locked and sleep periods ending after `after`, the open one included
        (to infinity): the bridge cuts browser time that falls in them."""
        with self._lock:
            periods = [p for p in self._away if p[1] > after]
            if self.cur and self.cur["type"] in AWAY:
                periods.append((self.cur["since"], math.inf))
        return periods

    def _check_gap(self, now, elapsed):
        if self.last is None:
            return
        last_wall, last_elapsed = self.last
        limit = self.cfg.poll_interval + 10
        if self.cur and self.cur["type"] == "sleep":
            return  # logind said we were going to sleep; that interval covers the gap
        if now - last_wall > limit and elapsed - last_elapsed > limit:
            self._close(last_wall)
            self._open("sleep", last_wall)
            self._close(now)
        elif now - last_wall > limit or now < last_wall:
            self._close(last_wall)

    def _probe(self, name):
        """An optional backend call (is_locked, sleep_started); None if absent or failing."""
        fn = getattr(self.backend, name, None)
        try:
            return fn() if fn else None
        except Exception:
            return None

    def _want(self, now):
        """What the machine is doing now: (type, since, app, title), or None for no
        window. Sleep beats locked beats idle beats the foreground window."""
        sleeping = self._probe("sleep_started")
        if sleeping:
            return "sleep", min(sleeping, now), None, None
        if self._probe("is_locked"):
            return "locked", now, None, None
        try:
            idle = self.backend.get_idle_seconds()
        except Exception:
            idle = 0.0
        if idle >= self.cfg.afk_threshold:
            return "idle", now - idle, None, None  # since input stopped
        try:
            app, title = self.backend.get_active_window()
        except Exception as e:
            app, title = None, None
            log(f"[tracker] active-window error: {e}")
        return ("app", now, app, title) if app is not None else None

    def tick(self):
        now = self.now()
        elapsed = self.clock()
        self._check_gap(now, elapsed)
        self.last = (now, elapsed)
        cur, want = self.cur, self._want(now)
        if want is None:
            self._close(now)
        elif not cur or (cur["type"], cur["app"], cur["title"]) != (want[0], want[2], want[3]):
            # idle and sleep start in the past (when input stopped / sleep was announced):
            # what was open ends there, or where its unsent part begins if that is later
            self._close(max(want[1], cur["start"]) if cur else want[1])
            self._open(*want)

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


def describe_backend(platform):
    """The backend in use, for the banner (NAME alone is set only once detected)."""
    describe = getattr(platform, "describe", None)
    return describe() if describe else platform.NAME


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.toml"))
    args = ap.parse_args()
    cfg = Config(args.config)

    sender = Sender(cfg, Spool(cfg.spool_path, cfg.spool_max_rows, cfg.spool_max_days))
    sender.start()

    # report real boot time once
    boot_id = getattr(backend, "boot_id", lambda: None)()
    sender.put(boot_event(cfg.device, getattr(backend, "boot_time", psutil.boot_time)(), boot_id))

    tracker = Tracker(cfg, sender.put_many, backend)
    bridge = BrowserBridge(cfg, sender, tracker)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    if not cfg.bridge_secret:
        log("[bridge] bridge_secret is not set in the config: the browser extension's "
            "posts will be refused")
    stop = threading.Event()
    configure = getattr(backend, "configure", None)
    if configure:  # Linux: idle threshold for the Wayland notification, wake-ups on changes
        configure(afk_threshold=cfg.afk_threshold, on_change=tracker.wake.set)

    def on_signal(signum, frame):
        stop.set()
        tracker.wake.set()

    # systemd stops with SIGTERM, a terminal with SIGINT/SIGHUP: in every case close the
    # open interval at "now" and put it in the spool before exiting
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    print(
        f"[xrnmoni] device={cfg.device} os={OS_NAME} backend={describe_backend(backend)} "
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
