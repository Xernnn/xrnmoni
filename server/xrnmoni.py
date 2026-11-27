#!/usr/bin/env python3
"""xrnmoni hub: keeps what every device's agent reports, and serves the dashboard.

Each monitored machine runs agent/agent.py, which posts here what was in the foreground, when the
machine was idle, locked or asleep, and the browser extension's pages. One hub holds every device.
It runs anywhere Python 3.10+ and `bcrypt` do (Linux, Windows, Termux on a phone): on its own
address, or behind an HTTPS reverse proxy that publishes it under a path (/xrnmoni), which may or
may not strip that prefix. Standard library only, plus `bcrypt` to read the users file.

    GET    /health, /healthz          200, no login (for monitoring)
    POST   /api/events                an agent's batch; Authorization: Bearer <device token>
                                      {"device", "os", "events": [{id, type, app, title, url,
                                      domain, ts_start, ts_end, duration}]}
                                      200 {"ok", "inserted", "duplicates",
                                           "rejected": [{"index", "id", "error"}]}
    POST   /api/session               with Basic auth and X-Xrn-Client: sets the sign-in cookie
    DELETE /api/session               signs this browser out, and ends its cookie for good
    GET    /api/devices               each device: when it was last heard from, what it's doing
    GET    /api/day?since=&until=     per device: the timeline, active time, apps, sites, categories
                                      (timeline=0: the figures without the timeline)
    GET    /api/days?bounds=t0,t1,..  per day (t0-t1, t1-t2, ...) and device: active time, categories
    GET    /api/detail?since=&until=&app=|site=|category=
                                      the window titles or pages behind one row, with time
    GET    /api/activity?since=&until=  the events themselves, newest first
    GET    /api/export?since=&until=&tz=  the same as CSV, times in zone tz
    GET    /api/summary?device=&since=&until=  totals in the shape the first dashboard read
    GET    /api/categories            the category rules; ETag: "<version>"
    PUT    /api/categories            replaces them; needs If-Match: "<version>" (or "*")
    GET    /api/changes?seq=&wait=    {"seq"}, as soon as it isn't [seq] or [wait] seconds pass
    GET    /api/tokens                the device tokens (the owner only): id, device, made, last post
    POST   /api/tokens                {"device"}: a new device token, answered once and never again
    DELETE /api/tokens/<id>           revokes one
    GET    /api/viewers               who can sign in (the owner only): the owner, then the monitors
    POST   /api/viewers               {"name", "password"}: adds a monitor who may watch, not manage
    DELETE /api/viewers/<name>        removes a monitor (the owner's own account stays)
    GET    /api/downloads             what the downloads folder holds, with size, SHA-256 and a link
                                      good for an hour; for a viewer or a device token
    GET    /downloads/<name>?k=       one of them, by that link
    GET    /  (and what it asks for)  the dashboard, from web/; /get is the downloads page
    HEAD                              any GET above, answered without its body

Every read also takes device=NAME for one device. Times are epoch seconds; the dashboard sends the
viewer's own midnights, so a day is the viewer's day. Reads need the sign-in cookie with an
X-Xrn-Client header, or Basic auth, from a viewer: the users file's first user (the owner), anyone
named with --viewer, and the monitors the owner adds on the People page (kept in viewers.json). A
monitor watches but manages nothing. Device tokens only post events.

    python3 xrnmoni.py --data DIR [--host 127.0.0.1] [--port 5240] [--users FILE] [--prefix /xrnmoni]
    python3 xrnmoni.py user add|remove|list [NAME] --data DIR [--users FILE]
    python3 xrnmoni.py token add DEVICE|list|revoke ID --data DIR

DIR holds the database, the device tokens, the category rules, the downloads/ folder the Get page
lists (the agents' installers, put there by hand) and, by default, the users file.
"""
import argparse
import base64
import csv
import fnmatch
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import secrets
import select
import socket
import sqlite3
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

MAX_BODY = 8 * 1024 * 1024  # an agent back after days offline sends what it kept, up to MAX_EVENTS
MAX_EVENTS = 10000
MAX_SPAN_S = 7 * 86400  # one event; a laptop asleep over a long weekend is still one
MAX_RANGE_S = 62 * 86400  # what one view reads
MAX_EXPORT_S = 400 * 86400
MAX_DAYS = 62
EXPORT_PAGE = 5000
EARLIEST = 946684800.0  # 2000-01-01: a clock that far back is wrong
AHEAD_S = 86400  # a clock that far ahead is wrong too
FIELD_MAX = {"app": 200, "title": 1000, "url": 4000, "domain": 253, "os": 32}
EVENT_ID = re.compile(r"[0-9a-f]{32}")
EVENT_TYPE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
LEGACY_TYPES = {"browser": "web", "afk": "idle"}  # what the first agent called them
AWAY = ("sleep", "locked", "idle")  # time nobody was at the device, strongest first
PRIVATE = "Private"  # what agents send for a private window's title and a private tab's page
DEVICE_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
MERGE_GAP_S = 5  # pieces of the same thing this close are one stretch on the timeline
TOP = 50
RETAIN_DAYS = 400
PRUNE_EVERY_S = 86400
WAIT_MAX_S = 30
PREFIX = "/xrnmoni"
DEFAULT_PORT = 5240
LOG_MAX = 5 * 1024 * 1024

AUTH_CACHE_S = 600  # a bcrypt check costs ~0.3s on a phone; don't pay it per request
FAIL_DELAY_S = 2  # after a wrong one, as Radicale does
MAX_HELD = 8  # wrong logins held for FAIL_DELAY_S at once; any more are answered at once
FAIL_LIMIT = 10  # different wrong logins from one address within FAIL_WINDOW_S, then 429
FAIL_WINDOW_S = 15 * 60
SESSION_COOKIE = "xrn_session"
SESSION_S = 30 * 86400
SIGNED_OUT = "signed-out.json"
USER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TOKEN = re.compile(r"xrnmoni_([0-9a-f]{8})_([A-Za-z0-9_-]{43})")
TOKEN_ID = re.compile(r"[0-9a-f]{8}")
MAX_TOKENS = 50
DOWNLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
DOWNLOAD_TYPES = {
    ".apk": "application/vnd.android.package-archive",
    ".zip": "application/zip",
    ".xpi": "application/x-xpinstall",
    ".gz": "application/gzip",
    ".exe": "application/vnd.microsoft.portable-executable",
}
# What each is for, by its name, in the order the Get page lists them.
DOWNLOAD_KINDS = (("Windows", lambda n: "windows" in n and n.endswith((".zip", ".exe"))),
                  ("Linux", lambda n: "linux" in n and n.endswith(".tar.gz")),
                  ("Android", lambda n: n.endswith(".apk")),
                  ("Chrome, Edge", lambda n: "extension" in n and n.endswith(".zip")),
                  ("Firefox", lambda n: n.endswith(".xpi")))
LINK_S = 3600  # how long a download link from /api/downloads works
MAX_RULES = 500
CATEGORY = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
PATTERN = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
TZ_NAME = re.compile(r"[A-Za-z0-9_+-]{1,32}(?:/[A-Za-z0-9_+-]{1,32}){0,2}")

WEB_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
WEB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
JSON = "application/json; charset=utf-8"
# The page's own files and requests to this server, nothing else: a window title that slipped
# past the page's escaping still couldn't run or send anything anywhere.
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
    ),
}

# The request line and X-Forwarded-For come from the caller. Written to the log as they are, a
# control character in them could rewrite the terminal the log is read in, or (a \r) start a line
# of its own for a log reader. Escaped as http.server's own log_message, replaced here, does.
LOG_ESCAPES = str.maketrans({**{c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))}, "\\": "\\\\"})


def log(line):
    if sys.stderr is not None:
        sys.stderr.write(line.translate(LOG_ESCAPES) + "\n")


class LogFile:
    """Where the log goes when there is no console (pythonw on Windows, a scheduled task): a file in
    the data folder, each line stamped, kept to two files of LOG_MAX. Without it the first line
    written to the missing stderr ended the server."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.f = open(path, "a", encoding="utf-8")

    def write(self, text):
        # Logging never stops a request: on Windows the rename fails while another program holds
        # the log open (an editor, Get-Content -Wait), and a full disk fails the write. The file
        # is opened again then, and the next line past LOG_MAX tries the rename again.
        with self.lock:
            try:
                if self.f.closed:
                    self.f = open(self.path, "a", encoding="utf-8")
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S ")
                lines = text.splitlines(keepends=True)
                self.f.write("".join(stamp + line for line in lines))
                self.f.flush()
                if self.f.tell() > LOG_MAX:
                    self.f.close()
                    try:
                        os.replace(self.path, self.path + ".1")
                    finally:
                        self.f = open(self.path, "a", encoding="utf-8")
            except OSError:
                pass
        return len(text)

    def flush(self):
        pass


def write_private(path, text):
    """Writes [text] whole to a new file that then takes [path]'s place, so a reader gets one
    version or the other, never half of one."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def lock_file(fd):
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # tries for 10 seconds, then raises
            return
        except OSError:
            continue


def unlock_file(fd):
    if not fcntl:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def file_stamp(path):
    """What changes whenever the file is written again (they're swapped in by rename), or None."""
    try:
        st = os.stat(path)
        return st.st_ino, st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


# ---- events -------------------------------------------------------------------------------------

class BadEvent(ValueError):
    pass


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BadEvent(f"{name} must be a number")
    return float(value)


def text_field(e, key):
    value = e.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadEvent(f"{key} must be text")
    return value[:FIELD_MAX[key]] or None


def domain_of(url):
    try:
        host = urlsplit(url).hostname if url else None
    except ValueError:
        return None
    return host.lower()[:FIELD_MAX["domain"]] if host else None


def clean_event(e, now):
    """(uid, type, app, title, url, domain, start, end, duration) for one event an agent sent, or
    BadEvent saying what is wrong with it. The first agent's `browser` and `afk` are stored as
    `web` and `idle`; a type this server doesn't know is kept as it is."""
    if not isinstance(e, dict):
        raise BadEvent("not an object")
    uid = e.get("id")
    if uid is not None:
        uid = uid.replace("-", "").lower() if isinstance(uid, str) else None
        if not uid or not EVENT_ID.fullmatch(uid):
            raise BadEvent("id must be 32 hex digits")
    kind = e.get("type", "app")
    if not isinstance(kind, str) or not EVENT_TYPE.fullmatch(kind):
        raise BadEvent("bad type")
    kind = LEGACY_TYPES.get(kind, kind)
    if e.get("ts_start") is None:
        raise BadEvent("ts_start missing")
    start = number(e["ts_start"], "ts_start")
    end = None if e.get("ts_end") is None else number(e["ts_end"], "ts_end")
    duration = None if e.get("duration") is None else number(e["duration"], "duration")
    if duration is not None and duration < 0:
        raise BadEvent("duration is negative")
    if end is None:
        end = start + (duration or 0.0)
    if not EARLIEST <= start <= now + AHEAD_S or end > now + AHEAD_S:
        raise BadEvent("time out of range")
    if end < start:
        raise BadEvent("ts_end is before ts_start")
    if end - start > MAX_SPAN_S:
        raise BadEvent("longer than 7 days")
    url = text_field(e, "url")
    domain = text_field(e, "domain")
    domain = domain.strip().lower() if domain else domain_of(url)
    return (uid, kind, text_field(e, "app"), text_field(e, "title"), url, domain or None,
            start, end, end - start if duration is None else duration)


SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT,
    device     TEXT NOT NULL,
    type       TEXT NOT NULL,
    app        TEXT,
    title      TEXT,
    url        TEXT,
    domain     TEXT,
    ts_start   REAL NOT NULL,
    ts_end     REAL,
    duration   REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device     TEXT PRIMARY KEY,
    os         TEXT,
    first_seen REAL,
    last_seen  REAL
);
"""


class Store:
    """The events, in SQLite (WAL, so a backup or the sqlite3 shell can read it while it runs).
    One connection behind a lock: a handful of devices and one viewer don't need more."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        with self.db:
            self.migrate()
        self.spans = dict(self.db.execute("SELECT device, max_span FROM devices"))

    def migrate(self):
        """Makes the tables, or brings the first server's xrnmoni.db up to date: an id column for
        dedupe, the old type names renamed, point events given an end, and each device's longest
        event noted (reads go back that far for what reaches into a day)."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "uid" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN uid TEXT")
        if "max_span" not in {r[1] for r in self.db.execute("PRAGMA table_info(devices)")}:
            self.db.execute("ALTER TABLE devices ADD COLUMN max_span REAL NOT NULL DEFAULT 0")
        for old, new in LEGACY_TYPES.items():
            self.db.execute("UPDATE events SET type=? WHERE type=?", (new, old))
        self.db.execute("UPDATE events SET ts_end = ts_start + MAX(COALESCE(duration, 0), 0) WHERE ts_end IS NULL")
        # Every read is one device's time range, and the type index served none of them.
        self.db.execute("DROP INDEX IF EXISTS idx_events_type")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device, ts_start)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_uid ON events(uid)")
        self.db.execute("""UPDATE devices SET max_span = COALESCE(
            (SELECT MAX(ts_end - ts_start) FROM events WHERE events.device = devices.device), 0)""")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def insert(self, device, os_name, rows, now):
        """Stores [rows] (from clean_event) for [device]; one already here by its id is left out.
        Returns how many were new."""
        span = max((r[7] - r[6] for r in rows), default=0.0)
        with self.lock, self.db:
            before = self.db.total_changes
            self.db.executemany(
                """INSERT OR IGNORE INTO events
                   (uid, device, type, app, title, url, domain, ts_start, ts_end, duration, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(r[0], device, *r[1:], now) for r in rows])
            inserted = self.db.total_changes - before
            self.db.execute(
                """INSERT INTO devices (device, os, first_seen, last_seen, max_span) VALUES (?,?,?,?,?)
                   ON CONFLICT(device) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       os = COALESCE(excluded.os, devices.os),
                       max_span = MAX(devices.max_span, excluded.max_span)""",
                (device, os_name, now, now, span))
            self.spans[device] = max(self.spans.get(device, 0.0), span)
        return inserted

    def devices(self):
        with self.lock:
            cur = self.db.execute("SELECT device, os, first_seen, last_seen FROM devices ORDER BY device")
            return [dict(zip(("device", "os", "first_seen", "last_seen"), r)) for r in cur]

    def latest(self, device):
        """The device's newest events, newest first."""
        with self.lock:
            return self.db.execute(
                """SELECT type, app, title, url, domain, ts_start, ts_end FROM events
                   WHERE device=? ORDER BY ts_start DESC LIMIT 50""", (device,)).fetchall()

    def rows(self, device, since, until):
        """(type, app, title, url, domain, start, end) of each event of [device] that reaches into
        [since, until), earliest first. An event that began before [since] counts too: reads start
        as far back as the device's longest event."""
        with self.lock:
            low = since - self.spans.get(device, 0.0) - 1
            return self.db.execute(
                """SELECT type, app, title, url, domain, ts_start, ts_end FROM events
                   WHERE device=? AND ts_start >= ? AND ts_start < ? AND ts_end >= ?
                   ORDER BY ts_start, id""", (device, low, until, since)).fetchall()

    def page(self, devices, since, until, after, limit):
        """Up to [limit] events of [devices] starting in [since, until), oldest first, after the
        (ts_start, id) [after]: an export reads in pages, so posts aren't held up behind it."""
        marks = ",".join("?" * len(devices))
        with self.lock:
            return self.db.execute(
                f"""SELECT id, uid, device, type, app, title, url, domain, ts_start, ts_end, duration
                    FROM events WHERE device IN ({marks}) AND ts_start >= ? AND ts_start < ?
                    AND (ts_start > ? OR (ts_start = ? AND id > ?))
                    ORDER BY ts_start, id LIMIT ?""",
                (*devices, since, until, after[0], after[0], after[1], limit)).fetchall()

    def newest(self, devices, since, until, limit):
        marks = ",".join("?" * len(devices))
        with self.lock:
            return self.db.execute(
                f"""SELECT uid, device, type, app, title, url, domain, ts_start, ts_end, duration
                    FROM events WHERE device IN ({marks}) AND ts_start >= ? AND ts_start < ?
                    ORDER BY ts_start DESC, id DESC LIMIT ?""", (*devices, since, until, limit)).fetchall()

    def prune(self, before):
        """Deletes every event that began before [before]; how many went."""
        gone = 0
        for d in self.devices():
            with self.lock, self.db:
                gone += self.db.execute("DELETE FROM events WHERE device=? AND ts_start < ?",
                                        (d["device"], before)).rowcount
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return gone


# ---- what a day adds up to ----------------------------------------------------------------------

def one_layer(pieces):
    """[pieces] (start, end, what), earliest first, made not to overlap: where two do, the one that
    began first keeps the time. The same interval sent twice by an agent too old to give ids is
    counted once that way."""
    out, edge = [], -math.inf
    for start, end, what in pieces:
        start = max(start, edge)
        if end > start:
            out.append((start, end, what))
            edge = end
    return out


def away_layer(pieces):
    """The stretches covered by any of [pieces] (start, end, kind), each named by the strongest
    kind over it (sleep, then locked, then idle), without overlaps."""
    marks = sorted([(s, 1, k) for s, e, k in pieces] + [(e, -1, k) for s, e, k in pieces])
    counts = dict.fromkeys(AWAY, 0)
    out, prev = [], None
    for t, step, kind in marks:
        if prev is not None and t > prev:
            top = next((k for k in AWAY if counts[k] > 0), None)
            if top:
                if out and out[-1][2] == top and out[-1][1] == prev:
                    out[-1] = (out[-1][0], t, top)
                else:
                    out.append((prev, t, top))
        counts[kind] += step
        prev = t
    return out


def add(table, key, seconds):
    table[key] = table.get(key, 0.0) + seconds


def ranked(table, limit=TOP):
    return [[k, round(v, 1)] for k, v in sorted(table.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]]


class Day:
    """What [rows] of one device add up to between [since] and [until].

    Time counts as active when an app or a page was in front and the device wasn't idle, locked or
    asleep: a page left open while nobody was there, or an app the agent saw in front of the lock
    screen, isn't time spent. An event reaching past either end counts only for its part inside."""

    def __init__(self, rows, since, until, category, want=None):
        self.category = category
        self.active = 0.0
        self.totals = {}
        self.apps, self.sites, self.categories = {}, {}, {}
        self.details = {}  # the titles or pages behind [want] = ("app"|"site"|"category", name)
        self.segments, self.web, self.marks = [], [], []
        self.first = self.last = None
        apps, webs, aways = [], [], []
        for kind, app, title, url, domain, start, end in rows:
            if end <= start:
                if since <= start < until:
                    self.marks.append([round(start, 1), kind])
                continue
            s, e = max(start, since), min(end, until)
            if e <= s:
                continue
            if kind == "app":
                apps.append((s, e, (app or "?", title or "")))
            elif kind == "web":
                # A private tab comes with no address: its time is the site "Private".
                site = domain or (PRIVATE if title == PRIVATE and not url else "?")
                webs.append((s, e, (site, title or "", url or "")))
            elif kind in AWAY:
                aways.append((s, e, kind))
            else:
                add(self.totals, kind, e - s)
        apps.sort(key=lambda p: p[0])
        webs.sort(key=lambda p: p[0])
        self.sweep(one_layer(apps), one_layer(webs), away_layer(aways), category, want)

    def sweep(self, apps, webs, aways, category, want):
        cuts = sorted({t for layer in (apps, webs, aways) for p in layer for t in p[:2]})
        at = [0, 0, 0]
        layers = (apps, webs, aways)
        for x, y in zip(cuts, cuts[1:]):
            now = []
            for n, layer in enumerate(layers):
                while at[n] < len(layer) and layer[at[n]][1] <= x:
                    at[n] += 1
                piece = layer[at[n]] if at[n] < len(layer) and layer[at[n]][0] <= x else None
                now.append(piece)
            app, web, away = now
            d = y - x
            if away:
                add(self.totals, away[2], d)
                self.stretch(self.segments, x, y, away[2], "", None)
                continue
            if not app and not web:
                continue
            self.active += d
            # The first and last moments someone was at it, not the night the device slept through.
            self.first = x if self.first is None else self.first
            self.last = y
            # A page's category, where it has one, says more than the browser's.
            cat, source = None, None
            if web:
                add(self.totals, "web", d)
                add(self.sites, web[2][0], d)
                cat = category("site", web[2][0])
                source = (web[2][0], "site")
            if app:
                add(self.totals, "app", d)
                add(self.apps, app[2][0], d)
                if not cat:
                    cat, source = category("app", app[2][0]), (app[2][0], "app")
            add(self.categories, cat or "", d)
            if web:
                self.stretch(self.web, x, y, "web", web[2][0], cat)
            self.stretch(self.segments, x, y, "app" if app else "web", (app or web)[2][0], cat)
            if want:
                self.detail(want, app, web, cat, source, d)

    def detail(self, want, app, web, cat, source, d):
        """Adds [d] seconds to the row [want] asks for: an app's window titles, a site's pages (by
        title, with the first address seen for it), or a category's apps and sites."""
        kind, name = want
        if kind == "app" and app and app[2][0] == name:
            key, extra = app[2][1], ""
        elif kind == "site" and web and web[2][0] == name:
            key, extra = web[2][1], web[2][2]
        elif kind == "category" and (cat or "") == name:
            key, extra = source
        else:
            return
        row = self.details.setdefault(key, [0.0, extra])
        row[0] += d

    @staticmethod
    def stretch(segments, x, y, state, label, cat):
        last = segments[-1] if segments else None
        if last and last[2] == state and last[3] == label and last[4] == cat and x - last[1] <= MERGE_GAP_S:
            last[1] = y
        else:
            segments.append([x, y, state, label, cat])

    def answer(self, timeline=True):
        doc = {
            "active": round(self.active, 1),
            "totals": {k: round(v, 1) for k, v in sorted(self.totals.items())},
            "apps": [[n, s, self.category("app", n)] for n, s in ranked(self.apps)],
            "sites": [[n, s, self.category("site", n)] for n, s in ranked(self.sites)],
            "categories": ranked(self.categories, MAX_RULES),
            "first": self.first,
            "last": self.last,
        }
        if timeline:
            r = lambda segs: [[round(s[0], 1), round(s[1], 1), *s[2:]] for s in segs]  # noqa: E731
            doc.update(segments=r(self.segments), web=[[s[0], s[1], s[3], s[4]] for s in r(self.web)],
                       marks=self.marks)
        return doc


# ---- categories ---------------------------------------------------------------------------------

class Unreadable(Exception):
    pass


def clean_rules(rules):
    """The rules as they'll be kept, or ValueError: [{"kind": "app"|"site", "pattern", "category"}]."""
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f"rules must be a list of at most {MAX_RULES}")
    out = []
    for r in rules:
        if not isinstance(r, dict) or r.get("kind") not in ("app", "site"):
            raise ValueError('each rule needs kind "app" or "site"')
        pattern, cat = r.get("pattern"), r.get("category")
        if not isinstance(pattern, str) or not PATTERN.fullmatch(pattern.strip()):
            raise ValueError("each rule needs a pattern of 1 to 200 characters")
        if not isinstance(cat, str) or not CATEGORY.fullmatch(cat.strip()):
            raise ValueError("each rule needs a category of 1 to 40 characters")
        out.append({"kind": r["kind"], "pattern": pattern.strip(), "category": cat.strip()})
    return out


class Categories:
    """The rules that put apps and sites into categories, in categories.json: {"version", "rules"}.
    The first that matches wins. A pattern matches a name case-insensitively and may use * and ?;
    a site pattern also matches its subdomains (youtube.com takes m.youtube.com)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seen = self.doc = None

    def read(self):
        """{"version", "rules"}, read again when the file changes; Unreadable for a damaged file:
        answered as empty, the dashboard would offer to save over the owner's rules with nothing."""
        stamp = file_stamp(self.path)
        with self.lock:
            if stamp == self.seen and self.doc is not None:
                return self.doc
            if stamp is None:
                doc = {"version": 0, "rules": []}
            else:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        doc = json.load(f)
                    if not isinstance(doc, dict) or type(doc.get("version")) is not int:
                        raise ValueError("not a rules file")
                    doc = {"version": doc["version"], "rules": clean_rules(doc.get("rules"))}
                except (OSError, ValueError, RecursionError) as e:
                    raise Unreadable(f"{self.path} can't be read: {e}") from e
            self.seen, self.doc = stamp, doc
            return doc

    def write(self, version, rules):
        write_private(self.path, json.dumps({"version": version, "rules": rules}, ensure_ascii=False, indent=1))

    def matcher(self):
        """category(kind, name): the category the rules give a name, or None. With rules that
        can't be read, nothing has a category (and the log says why)."""
        try:
            rules = self.read()["rules"]
        except Unreadable as e:
            log(str(e))
            rules = []
        compiled = [(r["kind"], r["pattern"].lower(), r["category"]) for r in rules]
        memo = {}

        def category(kind, name):
            key = (kind, name)
            if key not in memo:
                low = (name or "").lower()
                memo[key] = next((c for k, p, c in compiled if k == kind and (
                    fnmatch.fnmatchcase(low, p) or (kind == "site" and low.endswith("." + p)))), None)
            return memo[key]
        return category


# ---- device tokens ------------------------------------------------------------------------------

class Tokens:
    """What an agent posts with in place of a password, shaped `xrnmoni_<id>_<secret>`, one per device, made
    and revoked with `xrnmoni.py token`. Only a SHA-256 of the secret is kept, in tokens.json, so a
    copy of that file posts nothing; the secret is 32 random bytes, which no guessing reaches, so a
    fast hash is enough. A token posts events under its own device's name and reads nothing.

    The first server had one shared token in XRNMONI_TOKEN; it still posts, under the name the
    agent sends."""

    def __init__(self, path, legacy=""):
        self.path = path
        self.legacy = legacy
        self.lock = threading.Lock()
        self.writing = threading.Lock()  # add and revoke read the file and write it back
        self.seen = None
        self.found = []

    def read(self):
        stamp = file_stamp(self.path)
        with self.lock:
            if stamp == self.seen:
                return self.found
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            found = doc.get("tokens") if isinstance(doc, dict) else None
            found = [t for t in found if isinstance(t, dict)] if isinstance(found, list) else []
        except FileNotFoundError:
            found = []
        except (OSError, ValueError) as e:
            log(f"{self.path} can't be read, so the tokens read before it hold: {e}")
            return self.found
        with self.lock:
            self.seen, self.found = stamp, found
        return found

    def device_for(self, header):
        """(True, device) for a device token, (True, None) for the old shared token, (False, None)
        for anything else."""
        if not header or not header.startswith("Bearer "):
            return False, None
        given = header[7:].strip()
        m = TOKEN.fullmatch(given)
        if m:
            token_id, secret = m.groups()
            want = hashlib.sha256(secret.encode()).hexdigest()
            for t in self.read():
                if t.get("id") == token_id and hmac.compare_digest(str(t.get("sha256", "")), want):
                    return True, t.get("device")
            return False, None
        if self.legacy and hmac.compare_digest(given.encode(), self.legacy.encode()):
            return True, None
        return False, None

    def add(self, device):
        if not isinstance(device, str) or not DEVICE_NAME.fullmatch(device.strip()):
            raise ValueError("a device name is 1 to 64 characters, with no control characters")
        with self.writing:
            return self._add(device)

    def _add(self, device):
        found = self.read()
        if len(found) >= MAX_TOKENS:
            raise ValueError(f"there are {MAX_TOKENS} tokens already; revoke one first")
        taken = {t.get("id") for t in found}
        token_id = secrets.token_hex(4)
        while token_id in taken:
            token_id = secrets.token_hex(4)
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        entry = {"id": token_id, "device": device.strip(), "sha256": hashlib.sha256(secret.encode()).hexdigest(),
                 "created": int(time.time())}
        write_private(self.path, json.dumps({"tokens": [*found, entry]}, ensure_ascii=False, indent=1))
        return f"xrnmoni_{token_id}_{secret}"

    def revoke(self, token_id):
        with self.writing:
            return self._revoke(token_id)

    def _revoke(self, token_id):
        found = self.read()
        kept = [t for t in found if t.get("id") != token_id]
        if len(kept) == len(found):
            return False
        write_private(self.path, json.dumps({"tokens": kept}, ensure_ascii=False, indent=1))
        return True


class Monitors:
    """The people the owner added on the People page to watch the dashboard. Each is a name in the
    users file; this list, in viewers.json, says which of them may see the data without being able to
    manage anything. It persists, so a restart keeps them without a `--viewer` on the command line."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.writing = threading.Lock()
        self.seen = None
        self.found = []

    def read(self):
        try:
            stamp = os.stat(self.path).st_mtime_ns, os.path.getsize(self.path)
        except OSError:
            stamp = None
        with self.lock:
            if stamp == self.seen:
                return self.found
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            found = doc.get("viewers") if isinstance(doc, dict) else None
            found = ([v for v in found if isinstance(v, dict) and USER_NAME.fullmatch(str(v.get("name", "")))]
                     if isinstance(found, list) else [])
        except FileNotFoundError:
            found = []
        except (OSError, ValueError) as e:
            log(f"{self.path} can't be read, so the monitors read before it hold: {e}")
            return self.found
        with self.lock:
            self.seen, self.found = stamp, found
        return found

    def names(self):
        return {v["name"] for v in self.read()}

    def add(self, name):
        with self.writing:
            found = self.read()
            if any(v["name"] == name for v in found):
                return
            entry = {"name": name, "created": int(time.time())}
            write_private(self.path, json.dumps({"viewers": [*found, entry]}, ensure_ascii=False, indent=1))

    def remove(self, name):
        with self.writing:
            found = self.read()
            kept = [v for v in found if v["name"] != name]
            if len(kept) != len(found):
                write_private(self.path, json.dumps({"viewers": kept}, ensure_ascii=False, indent=1))


# ---- downloads ----------------------------------------------------------------------------------

class Downloads:
    """The files in DIR/downloads that the Get page offers: the agents' installers, the extension and
    the phone app, each with its size and SHA-256. A file is fetched by a link this server signed, good
    for LINK_S, so a plain link (a phone's download manager, which sends no login) can fetch it, while
    the list itself needs a viewer's login or a device token."""

    def __init__(self, folder):
        self.folder = folder
        self.key = secrets.token_bytes(32)  # this process's only: a restart ends the links
        self.lock = threading.Lock()
        self.sums = {}  # name -> (file stamp, sha256)

    @staticmethod
    def kind(name):
        low = name.lower()
        return next((label for label, test in DOWNLOAD_KINDS if test(low)), None)

    def mac(self, name, expires):
        return hmac.new(self.key, f"{name}\x1f{expires}".encode(), hashlib.sha256).hexdigest()[:32]

    def sha256(self, name, path, stamp):
        with self.lock:
            hit = self.sums.get(name)
        if hit and hit[0] == stamp:
            return hit[1]
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        with self.lock:
            self.sums[name] = (stamp, digest.hexdigest())
        return digest.hexdigest()

    def listing(self, now=None):
        """[{"name", "kind", "size", "sha256", "modified", "href"}]: the newest file of each kind, in
        DOWNLOAD_KINDS order. An older version left in the folder isn't offered."""
        expires = int((now or time.time()) + LINK_S)
        try:
            names = os.listdir(self.folder)
        except FileNotFoundError:
            return []
        newest = {}
        for name in names:
            kind = self.kind(name) if DOWNLOAD_NAME.fullmatch(name) else None
            path = os.path.join(self.folder, name)
            if not kind or not os.path.isfile(path):
                continue
            st = os.stat(path)
            if kind not in newest or (st.st_mtime, name) > newest[kind][0]:
                newest[kind] = ((st.st_mtime, name), path)
        out = []
        for label, _ in DOWNLOAD_KINDS:
            if label not in newest:
                continue
            (mtime, name), path = newest[label]
            stamp = file_stamp(path)
            out.append({"name": name, "kind": label, "size": stamp[2], "sha256": self.sha256(name, path, stamp),
                        "modified": int(mtime), "href": f"downloads/{name}?k={expires}.{self.mac(name, expires)}"})
        return out

    def path(self, name, key, now=None):
        """The file a signed link names, or None: a made-up, changed or run-out link opens nothing."""
        if not DOWNLOAD_NAME.fullmatch(name) or not self.kind(name):
            return None
        expires, _, mac = (key or "").partition(".")
        if not expires.isdigit() or int(expires) < (now or time.time()):
            return None
        if not hmac.compare_digest(mac, self.mac(name, int(expires))):
            return None
        path = os.path.join(self.folder, name)
        return path if os.path.isfile(path) else None


# ---- logins -------------------------------------------------------------------------------------

def load_session_key(path):
    """Made on first start by whichever server gets there first; any others sharing the users file
    read it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    for _ in range(50):  # another server may be writing it this very moment
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
        time.sleep(0.1)
    raise SystemExit(f"{path} is too short to sign with")


def read_names(users_path):
    """The user names an htpasswd file lists, first first: the first is the owner."""
    found = []
    try:
        with open(users_path, encoding="utf-8") as f:
            for line in f:
                name = line.strip().partition(":")[0]
                if USER_NAME.fullmatch(name):
                    found.append(name)
    except OSError:
        pass
    return found


def add_user(path, name, password):
    """Add a user to the htpasswd file at `path`, or change an existing one's password, keeping the
    file's order (so the first line — the owner — stays the owner). Returns True if the name was new.
    Raises ValueError on a bad name or a password under 8 characters."""
    if not isinstance(name, str) or not USER_NAME.fullmatch(name):
        raise ValueError("a name is letters, digits, . _ or -, starting with a letter or digit")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("use a password of 8 characters or more")
    import bcrypt

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    replaced, kept = False, []
    for line in lines:
        if line.partition(":")[0] == name:
            kept.append(f"{name}:{hashed}")
            replaced = True
        else:
            kept.append(line)
    if not replaced:
        kept.append(f"{name}:{hashed}")
    write_private(path, "".join(line + "\n" for line in kept))
    return not replaced


def remove_user(path, name):
    """Remove a user from the htpasswd file. Returns True if they were there."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return False
    kept = [line for line in lines if line.partition(":")[0] != name]
    if len(kept) == len(lines):
        return False
    write_private(path, "".join(line + "\n" for line in kept))
    return True


def basic_login(header):
    """(user, secret) from `Authorization: Basic ...`, or (None, None)."""
    if not header or not header.startswith("Basic "):
        return None, None
    try:
        user, _, secret = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return None, None
    return user, secret


def session_token(cookie_header):
    """The xrn_session cookie's value in a Cookie header, or ""."""
    token = ""
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            token = value
    return token


def session_cookie(value, max_age, secure=True):
    # Path=/: other xrn services on the same address share this sign-in when they share the users file.
    return (f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
            f"{'Secure; ' if secure else ''}SameSite=Strict")


class SignedOut:
    """The cookies signed out on any server that shares the users file: signed-out.json, beside it,
    {sha256 of the cookie: when it would have run out}. The other xrn servers keep the same list
    with their own copy of this, so signing out here ends the cookie there too."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seen = None
        self.listed = {}

    def read(self):
        """The file's list: {} with no file, None (and a line in the log) for one that can't be read."""
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as e:
            log(f"{self.path} can't be read, so the sign-outs read before it hold: {e}")
            return None
        return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, (int, float))}

    def hashes(self):
        try:
            st = os.stat(self.path)
            now = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            now = None
        except OSError as e:
            now = ("unreadable", e.errno)
        with self.lock:
            if now != self.seen:
                self.seen = now
                listed = self.read() if now is not None else {}
                if listed is not None:
                    self.listed = listed
            return self.listed

    def add(self, entries):
        """Puts {sha256: when it runs out} on the list, under a lock on signed-out.json.lock with the
        file read again inside it, so two servers signing out at once both keep theirs."""
        lock = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_file(lock)
            listed = self.read()
            if listed is None:
                listed = dict(self.listed)
            now = time.time()
            kept = {k: v for k, v in {**listed, **entries}.items() if v >= now}
            write_private(self.path, json.dumps(kept, separators=(",", ":")))
            st = os.stat(self.path)
            with self.lock:
                self.seen, self.listed = (st.st_ino, st.st_mtime_ns, st.st_size), kept
        finally:
            unlock_file(lock)
            os.close(lock)


class Login:
    """Basic auth against an htpasswd file of bcrypt hashes, the xrn_session cookie made from it,
    and the count of wrong logins per address."""

    def __init__(self, users_path):
        self.users_path = users_path
        folder = os.path.dirname(os.path.abspath(users_path))
        self.session_key = load_session_key(os.path.join(folder, "session.key"))
        self.signed_out = SignedOut(os.path.join(folder, SIGNED_OUT))
        self.cache = {}  # sha256(user:secret) -> (user, expires, users-file mtime)
        self.fail_lock = threading.Lock()
        self.fail_settled = threading.Condition(self.fail_lock)
        # client address -> {fail_id of a wrong Authorization: (monotonic time it last came, user
        # name, users_stamp when it was found wrong)}
        self.failures = {}
        self.fail_key = secrets.token_bytes(32)  # this process's only: no password can be looked up
        self.checking = {}  # client address -> how many of its logins are being checked right now
        self.held = threading.BoundedSemaphore(MAX_HELD)

    def users_mtime(self):
        try:
            return os.stat(self.users_path).st_mtime
        except OSError:
            return 0

    def users_stamp(self):
        """Which version of the users file this is: a password found wrong in one is wrong until
        it changes. None if it can't be read."""
        try:
            st = os.stat(self.users_path)
        except OSError:
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size

    def fail_id(self, header):
        """What a wrong login is remembered by: keyed, so neither it nor a hash anyone could try
        passwords against is kept."""
        return hmac.new(self.fail_key, header.encode(), hashlib.sha256).hexdigest()

    def known_wrong(self, addr, header, stamp):
        """Whether this address sent this very login within FAIL_WINDOW_S, it was wrong, and the
        users file hasn't changed since: then it is wrong again, and bcrypt needn't say so."""
        with self.fail_lock:
            f = self.failures.get(addr, {}).get(self.fail_id(header))
            return bool(f and stamp is not None and f[2] == stamp and time.monotonic() - f[0] < FAIL_WINDOW_S)

    def cached_basic(self, header):
        """The user name for a password already found right (the auth cache), else None: no check."""
        user, secret = basic_login(header)
        if user is None:
            return None
        hit = self.cache.get(hashlib.sha256(f"{user}:{secret}".encode()).hexdigest())
        return hit[0] if hit and hit[1] > time.time() and hit[2] == self.users_mtime() else None

    def check_basic(self, header):
        """The user name for a valid `Authorization: Basic ...`, else None."""
        user, secret = basic_login(header)
        if user is None or not USER_NAME.fullmatch(user):
            return None
        key = hashlib.sha256(f"{user}:{secret}".encode()).hexdigest()
        mtime = self.users_mtime()
        hit = self.cache.get(key)
        if hit and hit[1] > time.time() and hit[2] == mtime:
            return hit[0]
        import bcrypt  # only needed once someone logs in

        try:
            checked, spare = False, None
            with open(self.users_path, encoding="utf-8") as f:
                for line in f:
                    name, _, hashed = line.strip().partition(":")
                    spare = hashed or spare
                    if name == user and hashed:
                        checked = True
                        if bcrypt.checkpw(secret.encode(), hashed.encode()):
                            now = time.time()
                            if len(self.cache) > 1000:
                                self.cache = {k: v for k, v in self.cache.items() if v[1] > now}
                            self.cache[key] = (user, now + AUTH_CACHE_S, mtime)
                            return user
            if not checked and spare:
                # A name that isn't there takes as long as a wrong password for one that is, or
                # the time an answer takes says which names exist. The result is never used.
                bcrypt.checkpw(secret.encode(), spare.encode())
        except (OSError, ValueError):
            pass
        return None

    def password_tag(self, user):
        """A fingerprint of [user]'s hash, so a new password ends the old sessions."""
        try:
            with open(self.users_path, encoding="utf-8") as f:
                for line in f:
                    name, _, hashed = line.strip().partition(":")
                    if name == user and hashed:
                        return hashlib.sha256(hashed.encode()).hexdigest()
        except OSError:
            pass
        return None

    def session_mac(self, body, tag):
        return hmac.new(self.session_key, f"{body}|{tag}".encode(), hashlib.sha256).hexdigest()

    def make_session(self, user):
        who = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        tag = self.password_tag(user)
        expires = int(time.time()) + SESSION_S
        # Made from the name, the second it runs out and the password alone, a cookie made in the
        # second another was signed out would be that signed-out cookie: it runs out a second later.
        listed = self.signed_out.hashes()
        while True:
            body = f"{who}.{expires}"
            token = f"{body}.{self.session_mac(body, tag)}"
            if hashlib.sha256(token.encode()).hexdigest() not in listed:
                return token
            expires += 1

    def check_session(self, cookie_header):
        """The user a request's cookie names, if it's ours, unexpired, not signed out and the
        password unchanged."""
        token = session_token(cookie_header)
        try:
            who, expires, mac = token.split(".")
            user = base64.urlsafe_b64decode(who + "=" * (-len(who) % 4)).decode("utf-8")
            if int(expires) < time.time():
                return None
        except ValueError:
            return None
        if hashlib.sha256(token.encode()).hexdigest() in self.signed_out.hashes():
            return None
        tag = self.password_tag(user)
        if tag is None:
            return None
        try:
            return user if hmac.compare_digest(self.session_mac(f"{who}.{expires}", tag), mac) else None
        except TypeError:
            return None

    def end_session(self, cookie_header):
        """Signs out the session a request's cookie carries, for good: refused from then on by every
        server that shares it, until it would have run out anyway."""
        user = self.check_session(cookie_header)
        if not user:
            return None
        token = session_token(cookie_header)
        try:
            self.signed_out.add({hashlib.sha256(token.encode()).hexdigest(): int(token.split(".")[1])})
        except OSError as e:
            log(f"sign-out not kept: {e}")
        return user

    def recent_failures(self, addr, now):
        """This address's wrong logins within FAIL_WINDOW_S. Under fail_lock."""
        recent = {k: f for k, f in self.failures.get(addr, {}).items() if now - f[0] < FAIL_WINDOW_S}
        if recent:
            self.failures[addr] = recent
        else:
            self.failures.pop(addr, None)
        return recent

    def locked_out(self, addr):
        with self.fail_lock:
            return len(self.recent_failures(addr, time.monotonic())) >= FAIL_LIMIT

    def reserve_guess(self, addr, header):
        """Waits for a place to check a login from this address in; False once the address has used
        up its wrong logins. A login being checked holds a place until settle_guess, so guesses sent
        at once can't all pass the limit while the first are still in bcrypt; one that finds no
        place waits rather than being turned away. A wrong login sent again needs no place: it is
        counted already."""
        key = self.fail_id(header)
        with self.fail_settled:
            now = time.monotonic()
            if len(self.failures) > 10000:  # a flood from many addresses: forget the stale ones
                for a in [a for a, fs in self.failures.items()
                          if now - max((f[0] for f in fs.values()), default=0) >= FAIL_WINDOW_S]:
                    del self.failures[a]
            while True:
                recent = self.recent_failures(addr, time.monotonic())
                if len(recent) >= FAIL_LIMIT:
                    return False
                if key in recent or len(recent) + self.checking.get(addr, 0) < FAIL_LIMIT:
                    self.checking[addr] = self.checking.get(addr, 0) + 1
                    return True
                self.fail_settled.wait(1)

    def settle_guess(self, addr, header, right, stamp=None):
        """A login reserve_guess let through has been checked. A wrong one counts against the
        address, and the same one again counts once: that's an agent still sending a revoked token
        or a browser an old password, every few seconds, not someone guessing, and counting each
        would lock the owner out of the dashboard on the same machine. It is remembered with the
        users file's [stamp], so it isn't checked again while that stays the same (known_wrong). A
        right one clears that name's mistakes from this address, no one else's. None settles
        nothing."""
        name = (basic_login(header)[0] or "")[:64]
        with self.fail_settled:
            left = self.checking.get(addr, 1) - 1
            if left > 0:
                self.checking[addr] = left
            else:
                self.checking.pop(addr, None)
            if right is False:
                self.failures.setdefault(addr, {})[self.fail_id(header)] = (time.monotonic(), name, stamp)
            elif right:
                kept = {k: f for k, f in self.failures.get(addr, {}).items() if f[1] != name}
                if kept:
                    self.failures[addr] = kept
                else:
                    self.failures.pop(addr, None)
            self.fail_settled.notify_all()

    def hold_wrong_login(self):
        """Holds a wrong login's answer FAIL_DELAY_S, at most MAX_HELD at once: each is a thread."""
        if self.held.acquire(blocking=False):
            try:
                time.sleep(FAIL_DELAY_S)
            finally:
                self.held.release()


def client_address(handler):
    """Who a wrong login is counted for. Behind a local proxy that's the last X-Forwarded-For hop,
    the one the proxy added: anything before it came from the caller, who could make up a new
    address for every guess. The header is only trusted from a loopback peer, for the same reason.
    An IPv6 address counts as its /64: a line comes with at least that many."""
    addr = caller(handler)
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 4:
        return addr
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return f"{ipaddress.IPv6Address(int(ip) >> 64 << 64)}/64"


def caller(handler):
    peer = handler.client_address[0]
    forwarded = handler.headers.get("X-Forwarded-For") if handler.headers else None
    return forwarded.split(",")[-1].strip() if forwarded and peer in ("127.0.0.1", "::1") else peer


def from_another_site(headers):
    """Whether a request came from another site's page. A browser that was once given the password
    in its own sign-in box sends it again with a form any site posts here. The browser says where
    the page was in Sec-Fetch-Site; one too old for that still sends Origin with a POST, held against
    this host's name (or the proxy's X-Forwarded-Host). Agents send neither."""
    origin = (headers.get("Origin") or "").strip().lower()
    if origin.startswith(("chrome-extension://", "moz-extension://")):
        return False
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site:
        return site in ("cross-site", "same-site")
    scheme, _, host = origin.partition("://")
    if scheme not in ("http", "https"):
        return False  # no Origin, or "null"
    ours = [headers.get("Host") or "", *(headers.get("X-Forwarded-Host") or "").split(",")]
    return host_key(host) not in {host_key(h) for h in ours if h.strip()}


def host_key(host):
    return host.strip().lower().removesuffix(":443").removesuffix(":80")


# ---- the server ---------------------------------------------------------------------------------

class Changes:
    """A number that moves whenever an agent posts, for the dashboard's long poll. It starts from
    the clock, so a restarted server never repeats the number a page last saw."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = int(time.time() * 1000)

    def bump(self):
        with self.cond:
            self.seq += 1
            self.cond.notify_all()

    def wait(self, seen, timeout, gone):
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.seq == seen:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(1.0, left))
                if self.seq == seen and gone():
                    return None
            return self.seq


class App:
    def __init__(self, data, users=None, web=None, prefix=PREFIX, retain_days=RETAIN_DAYS, viewers=(),
                 legacy_token=None):
        self.data = data
        self.users = users or os.path.join(data, "users")
        # Monitors can be added and removed from the web only when this server owns the users file
        # (it lives in the data dir). A shared file (one other services sign in with) is managed on
        # the server with `user add`, so the web never writes another service's accounts.
        self.owns_users = os.path.dirname(os.path.abspath(self.users)) == os.path.abspath(data)
        self.web = web or os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        self.prefix = prefix.rstrip("/")
        self.retain_days = retain_days
        self.viewers = set(viewers)
        self.store = Store(os.path.join(data, "xrnmoni.db"))
        self.login = Login(self.users)
        if legacy_token is None:
            legacy_token = os.environ.get("XRNMONI_TOKEN", "").strip()
        if legacy_token == "change-me":
            log("XRNMONI_TOKEN is the example's change-me, so it posts nothing; make device tokens with: token add")
            legacy_token = ""
        elif legacy_token and len(legacy_token) < 16:
            log("XRNMONI_TOKEN is short; device tokens (token add) are safer")
        self.tokens = Tokens(os.path.join(data, "tokens.json"), legacy_token)
        self.categories = Categories(os.path.join(data, "categories.json"))
        self.categories_lock = threading.Lock()
        self.downloads = Downloads(os.path.join(data, "downloads"))
        self.monitors = Monitors(os.path.join(data, "viewers.json"))
        self.changes = Changes()
        self.pages = {}  # web file -> (stamp, ETag, bytes)

    def is_viewer(self, user):
        return user in self.viewers or user in self.monitors.names() or self.is_owner(user)

    def is_owner(self, user):
        """The users file's first user: the only one who makes and revokes device tokens."""
        return user is not None and user == next(iter(read_names(self.users)), None)

    def prune(self, now=None):
        if not self.retain_days:
            return 0
        gone = self.store.prune((now or time.time()) - self.retain_days * 86400)
        if gone:
            log(f"pruned {gone} events older than {self.retain_days} days")
        return gone

    def pruner(self):
        while True:
            try:
                self.prune()
            except sqlite3.Error as e:
                log(f"prune: {e}")
            time.sleep(PRUNE_EVERY_S)

    def device_names(self, only):
        names = [d["device"] for d in self.store.devices()]
        return [n for n in names if n == only] if only is not None else names

    def now_doing(self, device):
        """What [device] last said it was doing: its event that reached furthest, and the page that
        was in front during it."""
        latest = self.store.latest(device)
        spans = [r for r in latest if r[6] > r[5]]
        if not spans:
            return None
        top = max(spans, key=lambda r: (r[6], r[0] in AWAY, r[0] == "app"))
        doc = {"type": top[0], "app": top[1], "title": top[2], "since": top[5], "until": top[6]}
        if top[0] == "app":
            # A page counts only if it was in front during that same stretch: the browser's last
            # page stays the newest web event long after another app has taken the front.
            web = max((r for r in spans if r[0] == "web"), key=lambda r: r[6], default=None)
            if web and web[6] > top[5] + 1 and web[5] < top[6]:
                doc.update(domain=web[4], page=web[2])
        elif top[0] == "web":
            doc.update(domain=top[4], page=top[2])
        return doc


class Handler(BaseHTTPRequestHandler):
    server_version = "xrnmoni/1"
    sys_version = ""  # the Server header named the Python version too
    timeout = 120  # seconds of silence before a connection is dropped, so a stalled client can't hold a thread
    app = None

    def log_message(self, fmt, *a):
        headers = getattr(self, "headers", None)  # none yet on a request line that didn't parse
        who = "".join(caller(self).split()) or "-" if headers else "-"
        log(f"{who} {fmt % a}")

    def handle(self):
        # A caller hanging up is the network, not a fault here: one line says so.
        try:
            super().handle()
        except ConnectionError as e:
            self.close_connection = True
            self.log_message("connection dropped: %s", type(e).__name__)

    def parse_request(self):
        if not super().parse_request():
            return False
        # Before the login, which a page elsewhere can have the browser pass for it.
        if self.command not in ("GET", "HEAD", "OPTIONS") and from_another_site(self.headers):
            self.send(403, "refused: sent from another site's page")
            return False
        return True

    def route(self):
        path = self.path.partition("?")[0]
        prefix = self.app.prefix
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):] or "/"
        return path

    def query(self):
        # Blank values kept: category= is the time no rule names. A blank device is all of them.
        q = {k: v[0] for k, v in parse_qs(self.path.partition("?")[2], keep_blank_values=True).items()}
        if not q.get("device"):
            q.pop("device", None)
        return q

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Cache-Control", cache)
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        if code != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and code != 304:
            self.wfile.write(body)

    def send_json(self, doc, headers=None):
        self.send(200, json.dumps(doc, ensure_ascii=False, separators=(",", ":")), JSON, headers)

    def body(self):
        """The request's JSON, or None once an error has been answered."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self.send(411, "send a Content-Length")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self.send(400, "bad Content-Length")
            return None
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            self.send(413, "body missing or too large")
            return None
        raw = self.rfile.read(length)
        if len(raw) < length:
            # The connection closed part way, as a proxy closes this side when the caller's drops.
            self.close_connection = True
            self.send(400, "the upload stopped short")
            return None
        encoding = (self.headers.get("Content-Encoding") or "").strip().lower()
        if encoding == "gzip":
            try:
                inflate = zlib.decompressobj(wbits=31)
                raw = inflate.decompress(raw, MAX_BODY + 1)
            except zlib.error:
                self.send(400, "bad gzip")
                return None
            if len(raw) > MAX_BODY or inflate.unconsumed_tail:
                self.send(413, "body too large")
                return None
        elif encoding not in ("", "identity"):
            self.send(415, "send it plain or gzip")
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            self.send(400, "body must be JSON")
            return None

    def password(self):
        """The user of a right `Authorization: Basic`, or None once a 401 or 429 has been answered."""
        login = self.app.login
        addr = client_address(self)
        header = self.headers.get("Authorization")
        # Turned away unchecked, a login guesses nothing, so its 429 isn't held.
        if not (login.reserve_guess(addr, header) if header else not login.locked_out(addr)):
            self.send(429, "too many wrong passwords; try again later",
                      headers={"Retry-After": str(FAIL_WINDOW_S)})
            return None
        user, right, stamp = None, None, None
        if header:
            try:
                # A password already found right is a session, not a login, and settles nothing.
                user = login.cached_basic(header)
                if not user:
                    stamp = login.users_stamp()
                    if login.known_wrong(addr, header, stamp):
                        right = False  # sent again and again, it would cost a bcrypt check each time
                    else:
                        user = login.check_basic(header)
                        right = bool(user)
            finally:
                login.settle_guess(addr, header, right, stamp)
        if not user:
            if header:
                login.hold_wrong_login()
            # The page's own fetch must see a plain 401, not the browser's password box.
            challenge = {} if self.headers.get("X-Xrn-Client") else {"WWW-Authenticate": 'Basic realm="xrnmoni"'}
            self.send(401, "login required", headers=challenge)
        return user

    def viewer(self):
        """The signed-in viewer, or None once a refusal has been answered. The cookie counts only
        with X-Xrn-Client, which another site's page can't send."""
        user = None
        if not self.headers.get("Authorization") and self.headers.get("X-Xrn-Client"):
            user = self.app.login.check_session(self.headers.get("Cookie"))
        user = user or self.password()
        if user and not self.app.is_viewer(user):
            # The users file may be shared with other services and other people.
            self.send(403, "this account can't see the dashboard")
            return None
        return user

    def owner(self):
        """The signed-in owner, or None once a refusal has been answered."""
        user = self.viewer()
        if user and not self.app.is_owner(user):
            self.send(403, "only the owner manages devices")
            return None
        return user

    def device_token(self):
        """(device name or None, True) for a device token, or (None, False) once a refusal has been
        answered. Wrong tokens count as wrong logins."""
        login = self.app.login
        addr = client_address(self)
        header = self.headers.get("Authorization")
        if not (login.reserve_guess(addr, header) if header else not login.locked_out(addr)):
            self.send(429, "too many wrong tokens; try again later", headers={"Retry-After": str(FAIL_WINDOW_S)})
            return None, False
        ok, device = False, None
        if header:
            try:
                ok, device = self.app.tokens.device_for(header)
            finally:
                login.settle_guess(addr, header, None if ok else False)
        if not ok:
            if header:
                login.hold_wrong_login()
            self.send(401, "bad or missing token", headers={"WWW-Authenticate": 'Bearer realm="xrnmoni"'})
            return None, False
        return device, True

    def secure_cookie(self):
        """Whether the sign-in cookie can be Secure: everywhere but a plain-HTTP connection straight
        from another machine (a hub on a LAN or tailnet address), where the browser would drop a
        Secure cookie and the sign-in would never stick. A proxy in front is on loopback."""
        return self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0].startswith("127.")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.route()
        if path == "/health":
            return self.send(200, "ok")
        if path == "/healthz":
            return self.send(200, '{"ok":true}', JSON)
        if path.startswith("/downloads/"):
            return self.send_download(path[len("/downloads/"):])
        if not path.startswith("/api/"):
            return self.serve_web(path)
        if path == "/api/downloads":
            return self.get_downloads()
        if path == "/api/tokens":
            return self.get_tokens() if self.owner() else None
        if path == "/api/viewers":
            return self.get_viewers() if self.owner() else None
        reads = {"/api/devices": self.get_devices, "/api/day": self.get_day, "/api/days": self.get_days,
                 "/api/detail": self.get_detail, "/api/activity": self.get_activity,
                 "/api/export": self.get_export, "/api/summary": self.get_summary,
                 "/api/categories": self.get_categories, "/api/changes": self.get_changes}
        if path not in reads:
            return self.send(404, "not found")
        if not self.viewer():
            return None
        try:
            return reads[path](self.query())
        except Unreadable as e:
            log(str(e))
            return self.send(503, "the category rules can't be read")
        except sqlite3.Error as e:
            log(f"database: {e}")
            return self.send(503, "the database can't be read")
        except BadQuery as e:
            return self.send(400, str(e))

    def do_POST(self):
        path = self.route()
        if path == "/api/session":
            if not self.headers.get("X-Xrn-Client"):
                return self.send(400, "X-Xrn-Client required")
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self.send(401, "login required")
            user = self.viewer()
            if not user:
                return None
            cookie = session_cookie(self.app.login.make_session(user), SESSION_S, self.secure_cookie())
            return self.send(200, json.dumps({"ok": True, "user": user}), JSON, {"Set-Cookie": cookie})
        if path == "/api/tokens":
            return self.post_token()
        if path == "/api/viewers":
            return self.post_viewer()
        if path != "/api/events":
            return self.send(404, "not found")
        device, ok = self.device_token()
        if not ok:
            return None
        doc = self.body()
        if doc is None:
            return None
        return self.post_events(device, doc)

    def post_events(self, bound, doc):
        events = doc.get("events") if isinstance(doc, dict) else None
        if not isinstance(events, list):
            return self.send(400, 'body must be {"device", "os", "events": [...]}')
        if len(events) > MAX_EVENTS:
            return self.send(400, f"at most {MAX_EVENTS} events at a time")
        device = doc.get("device")
        if bound is None and (not isinstance(device, str) or not DEVICE_NAME.fullmatch(device.strip())):
            return self.send(400, "device must be a name of 1 to 64 characters")
        # A device token posts as its own device, whatever the body says: a token taken from one
        # machine can't write another's day.
        device = bound or device.strip()
        os_name = doc.get("os")
        os_name = os_name[:FIELD_MAX["os"]] if isinstance(os_name, str) and os_name else None
        now = time.time()
        rows, rejected = [], []
        for i, e in enumerate(events):
            try:
                rows.append(clean_event(e, now))
            except BadEvent as err:
                uid = e.get("id") if isinstance(e, dict) and isinstance(e.get("id"), str) else None
                rejected.append({"index": i, "id": uid[:64] if uid else None, "error": str(err)})
        try:
            inserted = self.app.store.insert(device, os_name, rows, now)
        except sqlite3.Error as e:
            log(f"database: {e}")
            return self.send(503, "the database can't be written")
        self.app.changes.bump()
        return self.send_json({"ok": True, "inserted": inserted, "duplicates": len(rows) - inserted,
                               "rejected": rejected})

    def do_PUT(self):
        if self.route() != "/api/categories":
            return self.send(404, "not found")
        if not self.viewer():
            return None
        doc = self.body()
        if doc is None:
            return None
        try:
            rules = clean_rules(doc.get("rules") if isinstance(doc, dict) else None)
        except ValueError as e:
            return self.send(400, str(e))
        want = (self.headers.get("If-Match") or "").strip()
        if not want:
            return self.send(428, "If-Match required")
        app = self.app
        with app.categories_lock:
            try:
                cur = app.categories.read()
            except Unreadable as e:
                log(str(e))
                return self.send(503, "the category rules can't be read")
            if want != "*" and want.removeprefix("W/").strip('"') != str(cur["version"]):
                return self.send(412, "changed since you read it", headers={"ETag": f'"{cur["version"]}"'})
            version = cur["version"] + 1
            app.categories.write(version, rules)
        return self.send_json({"version": version, "rules": rules}, {"ETag": f'"{version}"'})

    def post_token(self):
        """Makes a device token for a new device, for the owner: answered this once, kept only as a hash."""
        if not self.owner():
            return None
        doc = self.body()
        if doc is None:
            return None
        device = doc.get("device") if isinstance(doc, dict) else None
        try:
            token = self.app.tokens.add(device)
        except ValueError as e:
            return self.send(400, str(e))
        except OSError as e:
            log(f"tokens: {e}")
            return self.send(503, "the tokens can't be written")
        return self.send_json({"id": token[8:16], "device": device.strip(), "token": token})

    def get_viewers(self):
        """The people who can sign in: the owner, then the monitors the owner added. For the owner only."""
        names = read_names(self.app.users)
        added = {v["name"]: v.get("created") for v in self.app.monitors.read()}
        people = [{"name": name, "owner": i == 0, "added": (None if i == 0 else added.get(name))}
                  for i, name in enumerate(names)]
        return self.send_json({"people": people, "manageable": self.app.owns_users})

    def post_viewer(self):
        """Adds a monitor: a name and password the owner gives, who may then watch the dashboard but
        manage nothing. For the owner only, and only when this server owns the users file."""
        if not self.owner():
            return None
        if not self.app.owns_users:
            return self.send(409, "this dashboard shares its sign-in with other services; add a monitor "
                                  "on the server with: user add")
        doc = self.body()
        if doc is None:
            return None
        name = doc.get("name") if isinstance(doc, dict) else None
        password = doc.get("password") if isinstance(doc, dict) else None
        if isinstance(name, str) and name == next(iter(read_names(self.app.users)), None):
            return self.send(400, "that is the owner's own account")
        try:
            add_user(self.app.users, name, password)
        except ValueError as e:
            return self.send(400, str(e))
        except OSError as e:
            log(f"viewers: {e}")
            return self.send(503, "the account can't be written")
        self.app.monitors.add(name)
        return self.send_json({"name": name})

    def delete_viewer(self, name):
        """Removes a monitor: their account and their place on the viewers list. For the owner only.
        The owner's own account stays."""
        if not self.owner():
            return None
        if not self.app.owns_users:
            return self.send(409, "manage who signs in on the server with: user remove")
        if not USER_NAME.fullmatch(name):
            return self.send(404, "no such monitor")
        if name == next(iter(read_names(self.app.users)), None):
            return self.send(400, "the owner's own account stays")
        gone = remove_user(self.app.users, name)
        self.app.monitors.remove(name)
        if not gone:
            return self.send(404, "no such monitor")
        return self.send_json({"ok": True})

    def do_DELETE(self):
        path = self.route()
        if path.startswith("/api/tokens/"):
            if not self.owner():
                return None
            token_id = path[len("/api/tokens/"):]
            if not TOKEN_ID.fullmatch(token_id) or not self.app.tokens.revoke(token_id):
                return self.send(404, "no such token")
            return self.send_json({"ok": True})
        if path.startswith("/api/viewers/"):
            return self.delete_viewer(path[len("/api/viewers/"):])
        if path != "/api/session":
            return self.send(404, "not found")
        # Emptying the browser's cookie alone left any copy of it signing in for its full 30 days.
        self.app.login.end_session(self.headers.get("Cookie"))
        return self.send(200, '{"ok":true}', JSON, {"Set-Cookie": session_cookie("", 0, self.secure_cookie())})

    # -- reads --

    def get_devices(self, q):
        store = self.app.store
        out = []
        for d in store.devices():
            if q.get("device") is not None and d["device"] != q["device"]:
                continue
            out.append({**d, "now": self.app.now_doing(d["device"])})
        return self.send_json({"devices": out, "now": time.time(), "seq": self.app.changes.seq,
                               "owner": self.app.is_owner(self.viewer())})

    def get_day(self, q):
        since, until = time_range(q, MAX_RANGE_S)
        category = self.app.categories.matcher()
        timeline = q.get("timeline") != "0"
        out = []
        for d in self.app.store.devices():
            if q.get("device") is not None and d["device"] != q["device"]:
                continue
            day = Day(self.app.store.rows(d["device"], since, until), since, until, category)
            out.append({"device": d["device"], "os": d["os"], **day.answer(timeline)})
        return self.send_json({"since": since, "until": until, "devices": out})

    def get_days(self, q):
        try:
            bounds = [float(b) for b in q.get("bounds", "").split(",")]
        except ValueError:
            raise BadQuery("bounds must be numbers") from None
        if not 2 <= len(bounds) <= MAX_DAYS + 1 or any(not math.isfinite(b) for b in bounds) \
                or any(b >= c for b, c in zip(bounds, bounds[1:])) or bounds[-1] - bounds[0] > MAX_RANGE_S:
            raise BadQuery(f"bounds must be 2 to {MAX_DAYS + 1} rising times, at most 62 days apart")
        category = self.app.categories.matcher()
        days = [{"since": b, "until": c, "devices": {}} for b, c in zip(bounds, bounds[1:])]
        for name in self.app.device_names(q.get("device")):
            rows = self.app.store.rows(name, bounds[0], bounds[-1])
            for day in days:
                part = [r for r in rows if r[5] < day["until"] and r[6] >= day["since"]]
                if part:
                    doc = Day(part, day["since"], day["until"], category).answer(timeline=False)
                    day["devices"][name] = {"active": doc["active"], "totals": doc["totals"],
                                            "categories": doc["categories"], "apps": doc["apps"][:5],
                                            "sites": doc["sites"][:5]}
        return self.send_json({"days": days})

    def get_detail(self, q):
        since, until = time_range(q, MAX_RANGE_S)
        want = next(((k, q[k]) for k in ("app", "site", "category") if k in q), None)
        if want is None:
            raise BadQuery("give app=, site= or category=")
        category = self.app.categories.matcher()
        details = {}
        for name in self.app.device_names(q.get("device")):
            day = Day(self.app.store.rows(name, since, until), since, until, category, want)
            for key, (seconds, extra) in day.details.items():
                row = details.setdefault(key, [0.0, extra])
                row[0] += seconds
        rows = sorted(details.items(), key=lambda kv: (-kv[1][0], kv[0]))[:200]
        return self.send_json({"kind": want[0], "name": want[1],
                               "total": round(sum(v[0] for v in details.values()), 1),
                               "rows": [[k, v[1], round(v[0], 1)] for k, v in rows]})

    def get_activity(self, q):
        since, until = time_range(q, MAX_RANGE_S)
        try:
            limit = max(1, min(int(q.get("limit", 2000)), 20000))
        except ValueError:
            raise BadQuery("limit must be a number") from None
        names = self.app.device_names(q.get("device"))
        rows = self.app.store.newest(names, since, until, limit) if names else []
        keys = ("id", "device", "type", "app", "title", "url", "domain", "ts_start", "ts_end", "duration")
        return self.send_json({"events": [dict(zip(keys, r)) for r in rows]})

    def get_summary(self, q):
        """The first dashboard's totals for one device, kept for anything that still reads them."""
        since, until = time_range(q, MAX_RANGE_S)
        name = q.get("device")
        if not name:
            raise BadQuery("give device=")
        day = Day(self.app.store.rows(name, since, until), since, until, lambda k, n: None).answer(timeline=False)
        return self.send_json({
            "apps": [{"name": n, "seconds": s} for n, s, _ in day["apps"]],
            "domains": [{"name": n, "seconds": s} for n, s, _ in day["sites"]],
            "totals": {"app_seconds": day["totals"].get("app", 0), "web_seconds": day["totals"].get("web", 0),
                       "afk_seconds": day["totals"].get("idle", 0), "first_ts": day["first"], "last_ts": day["last"]},
        })

    def get_export(self, q):
        """The events as CSV, streamed in pages. Times are written in zone tz= (the viewer's), or the
        server's own zone where that zone isn't known here. A cell that a spreadsheet would read as a
        formula gets a ' in front: window and page titles are chosen by whatever was open."""
        since, until = time_range(q, MAX_EXPORT_S)
        zone = None
        tz = q.get("tz", "")
        if TZ_NAME.fullmatch(tz):
            try:
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(tz)
            except Exception:  # no zone database here (Windows without tzdata)
                zone = None
        names = self.app.device_names(q.get("device"))

        def when(ts):
            if ts is None:
                return ""
            moment = datetime.fromtimestamp(ts, timezone.utc)
            return (moment.astimezone(zone) if zone else moment.astimezone()).isoformat(timespec="seconds")

        stamp = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="xrnmoni-{stamp}.csv"')
        self.end_headers()
        self.close_connection = True  # the end of the answer is the end of the connection
        if self.command == "HEAD":
            return None
        buf = io.StringIO()
        out = csv.writer(buf, lineterminator="\r\n")
        out.writerow(["device", "type", "start", "end", "seconds", "app", "title", "domain", "url", "id"])
        after, rows = (-math.inf, 0), [None] * EXPORT_PAGE
        while names and len(rows) == EXPORT_PAGE:
            rows = self.app.store.page(names, since, until, after, EXPORT_PAGE)
            for rid, uid, device, kind, app, title, url, domain, start, end, duration in rows:
                seconds = (end - start) if end is not None else (duration or 0)
                out.writerow([cell(device), kind, when(start), when(end), round(seconds, 1), cell(app),
                              cell(title), cell(domain), cell(url), uid or ""])
                after = (start, rid)
            self.wfile.write(buf.getvalue().encode("utf-8"))
            buf.seek(0)
            buf.truncate()
        self.wfile.write(buf.getvalue().encode("utf-8"))
        return None

    def get_tokens(self):
        heard = {d["device"]: d["last_seen"] for d in self.app.store.devices()}
        return self.send_json({"tokens": [
            {"id": t.get("id"), "device": t.get("device"), "created": t.get("created"),
             "last_seen": heard.get(t.get("device"))} for t in self.app.tokens.read()]})

    def get_downloads(self):
        """The Get page's list: for a viewer, or a device token, so a child's device can be set up
        with the token it will post with, never the owner's password."""
        if (self.headers.get("Authorization") or "").startswith("Bearer "):
            if not self.device_token()[1]:
                return None
        elif not self.viewer():
            return None
        try:
            files = self.app.downloads.listing()
        except OSError as e:
            log(f"downloads: {e}")
            return self.send(503, "the downloads can't be read")
        return self.send_json({"files": files})

    def send_download(self, name):
        path = self.app.downloads.path(name, self.query().get("k"))
        if not path:
            return self.send(403, "link expired")
        try:
            f = open(path, "rb")
        except OSError:
            return self.send(404, "not found")
        with f:
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            for k, v in BASE_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", DOWNLOAD_TYPES[os.path.splitext(name)[1].lower()])
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            if self.command == "HEAD":
                return None
            for block in iter(lambda: f.read(1 << 16), b""):
                self.wfile.write(block)
        return None

    def get_categories(self, q):
        doc = self.app.categories.read()
        etag = f'"{doc["version"]}"'
        if (self.headers.get("If-None-Match") or "").strip().removeprefix("W/") == etag:
            return self.send(304, headers={"ETag": etag})
        return self.send_json(doc, {"ETag": etag})

    def get_changes(self, q):
        """Answers once an agent has posted since the page's [seq], or after [wait] seconds; ends
        unanswered if the page went away meanwhile."""
        try:
            seen = int(q.get("seq", "0"))
            wait = max(0.0, min(float(q.get("wait", "0")), WAIT_MAX_S))
        except ValueError:
            raise BadQuery("seq and wait must be numbers") from None
        seq = self.app.changes.wait(seen, wait, self.caller_gone)
        if seq is None:
            self.close_connection = True
            return None
        return self.send_json({"seq": seq})

    def caller_gone(self):
        """Whether the caller has closed its end: nothing to read but the end of the stream."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def serve_web(self, path):
        name = "index.html" if path in ("", "/") else "get.html" if path == "/get" else path.lstrip("/")
        if not WEB_NAME.fullmatch(name) or ".." in name:
            return self.send(404, "not found")
        ctype = WEB_TYPES.get(os.path.splitext(name)[1])
        full = os.path.join(self.app.web, name)
        stamp = file_stamp(full) if ctype else None
        if stamp is None:
            return self.send(404, "not found")
        hit = self.app.pages.get(name)
        if not hit or hit[0] != stamp:
            with open(full, "rb") as f:
                body = f.read()
            if name.endswith(".html"):
                # Every path the page uses is relative to this, so it works under any prefix.
                body = body.replace(b'<base href="/xrnmoni/">', f'<base href="{self.app.prefix}/">'.encode())
            hit = (stamp, '"' + hashlib.sha256(body).hexdigest()[:16] + '"', body)
            self.app.pages[name] = hit
        if self.headers.get("If-None-Match") == hit[1]:
            return self.send(304, headers={"ETag": hit[1]}, cache="no-cache")
        return self.send(200, hit[2], ctype, {"ETag": hit[1]}, cache="no-cache")


class BadQuery(ValueError):
    pass


def time_range(q, limit):
    """(since, until) from a query, the last day when it names none."""
    now = time.time()
    try:
        since = float(q.get("since", now - 86400))
        until = float(q.get("until", now))
    except ValueError:
        raise BadQuery("since and until must be numbers") from None
    if not (math.isfinite(since) and math.isfinite(until)) or until <= since or until - since > limit:
        raise BadQuery(f"until must come after since, at most {int(limit // 86400)} days later")
    return since, until


def cell(value):
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value or ""


class Server(ThreadingHTTPServer):
    """Quiet about callers that hang up: one line, not socketserver's traceback."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second copy bind the same port and take half the requests.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            log(f"{client_address[0]} connection dropped: {error!r}")
            return
        if sys.stderr is not None:
            super().handle_error(request, client_address)


# ---- command line -------------------------------------------------------------------------------

def users_file(args):
    return os.path.abspath(os.path.expanduser(args.users)) if args.users else os.path.join(args.data, "users")


def read_password():
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass
        first = getpass.getpass("Password: ")
        if first != getpass.getpass("Again: "):
            sys.exit("the two passwords differ")
        return first
    return (sys.stdin.readline() if sys.stdin else "").rstrip("\r\n")


def user_cli(argv):
    p = argparse.ArgumentParser(prog="xrnmoni.py user", description="Who can sign in to the dashboard.")
    p.add_argument("action", choices=("add", "remove", "list"))
    p.add_argument("name", nargs="?")
    p.add_argument("--data", required=True)
    p.add_argument("--users", help="an htpasswd file of bcrypt hashes (default: DIR/users)")
    args = p.parse_args(argv)
    args.data = os.path.abspath(os.path.expanduser(args.data))
    os.makedirs(args.data, mode=0o700, exist_ok=True)
    path = users_file(args)
    if args.action == "list":
        for i, name in enumerate(read_names(path)):
            print(name + ("  (owner)" if i == 0 else ""))
        return
    if not args.name or not USER_NAME.fullmatch(args.name):
        sys.exit("give a name of letters, digits, . _ or -, starting with a letter or digit")
    if args.action == "remove":
        if not remove_user(path, args.name):
            sys.exit(f"{args.name} isn't in {path}")
        print(f"Removed {args.name}.", file=sys.stderr)
        return
    password = read_password()
    try:
        add_user(path, args.name, password)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{args.name} can sign in{'' if read_names(path)[0] == args.name else ' once added as a monitor (People page) or named with --viewer'}.",
          file=sys.stderr)


def token_cli(argv):
    p = argparse.ArgumentParser(prog="xrnmoni.py token", description="Device tokens, which agents post with.")
    p.add_argument("action", choices=("add", "list", "revoke"))
    p.add_argument("what", nargs="?", help="add: the device's name, as the dashboard shows it; revoke: the id")
    p.add_argument("--data", required=True)
    args = p.parse_args(argv)
    data = os.path.abspath(os.path.expanduser(args.data))
    os.makedirs(data, mode=0o700, exist_ok=True)
    store = Tokens(os.path.join(data, "tokens.json"))
    if args.action == "add":
        if not args.what:
            sys.exit("give the device a name, such as: token add laptop")
        try:
            text = store.add(args.what)
        except ValueError as e:
            sys.exit(str(e))
        print(text)
        print(f"Put it in that device's agent config as token. It isn't shown again; revoke it with: "
              f"token revoke {text[8:16]}", file=sys.stderr)
    elif args.action == "list":
        for t in store.read():
            made = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("created", 0)))
            print(f"{t.get('id')}  {made}  {t.get('device')}")
    else:
        if not args.what or not TOKEN_ID.fullmatch(args.what):
            sys.exit("give the id that `token list` shows")
        if not store.revoke(args.what):
            sys.exit(f"no token {args.what}")
        print(f"Revoked {args.what}.", file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    if argv[:1] == ["user"]:
        return user_cli(argv[1:])
    if argv[:1] == ["token"]:
        return token_cli(argv[1:])
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1", help="the address to listen on (0.0.0.0: every one)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--data", required=True, help="where the database, tokens and rules live")
    p.add_argument("--users", help="an htpasswd file of bcrypt hashes to sign in with (default: DIR/users)")
    p.add_argument("--viewer", action="append", default=[],
                   help="another user in the users file who may see the dashboard (the first always may)")
    p.add_argument("--prefix", default=PREFIX, help="the path a reverse proxy publishes it under")
    p.add_argument("--retain-days", type=int, default=RETAIN_DAYS, help="events older than this go; 0 keeps all")
    p.add_argument("--web", help="the dashboard's folder (default: web/ beside this file)")
    args = p.parse_args(argv)
    args.data = os.path.abspath(os.path.expanduser(args.data))
    os.makedirs(args.data, mode=0o700, exist_ok=True)
    if sys.stderr is None:  # pythonw, or a task with no console
        sys.stderr = LogFile(os.path.join(args.data, "xrnmoni.log"))
    if sys.stdout is None:
        sys.stdout = sys.stderr
    prefix = "/" + args.prefix.strip("/") if args.prefix.strip("/") else ""
    app = App(args.data, users_file(args), os.path.expanduser(args.web) if args.web else None, prefix,
              max(0, args.retain_days), args.viewer)
    Handler.app = app
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        log(f"can't listen on {args.host}:{args.port}: {e}")
        sys.exit(1)
    threading.Thread(target=app.pruner, daemon=True).start()
    if not read_names(app.users):
        log(f"no one can sign in yet: python3 xrnmoni.py user add NAME --data {args.data}"
            + (f" --users {app.users}" if args.users else ""))
    log(f"xrnmoni on http://{args.host}:{args.port}, data in {args.data}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
