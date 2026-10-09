"""SQLite storage for xrnmoni.

One connection, guarded by a lock — fine for a personal, low-traffic tool.
All timestamps are epoch seconds (float).
"""
import os
import time
import sqlite3
import threading
from urllib.parse import urlsplit

DB_PATH = os.environ.get(
    "XRNMONI_DB", os.path.join(os.path.dirname(__file__), "xrnmoni.db")
)

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device     TEXT    NOT NULL,
    type       TEXT    NOT NULL,          -- app | browser | boot | shutdown | afk | heartbeat
    app        TEXT,
    title      TEXT,
    url        TEXT,
    domain     TEXT,
    ts_start   REAL    NOT NULL,
    ts_end     REAL,
    duration   REAL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device, ts_start);
CREATE INDEX IF NOT EXISTS idx_events_type      ON events(type);

CREATE TABLE IF NOT EXISTS devices (
    device     TEXT PRIMARY KEY,
    os         TEXT,
    first_seen REAL,
    last_seen  REAL
);
"""


def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _domain(url):
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
        return host.lower() if host else None
    except Exception:
        return None


def insert_events(device, os_name, events):
    """events: list of dicts. Returns number inserted."""
    conn = get_conn()
    now = time.time()
    rows = []
    for e in events:
        ts_start = float(e.get("ts_start") or now)
        ts_end = e.get("ts_end")
        ts_end = float(ts_end) if ts_end is not None else None
        duration = e.get("duration")
        if duration is None and ts_end is not None:
            duration = max(0.0, ts_end - ts_start)
        url = e.get("url")
        rows.append(
            (
                device,
                e.get("type", "app"),
                e.get("app"),
                e.get("title"),
                url,
                e.get("domain") or _domain(url),
                ts_start,
                ts_end,
                duration,
                now,
            )
        )
    with _lock:
        conn.executemany(
            """INSERT INTO events
               (device, type, app, title, url, domain, ts_start, ts_end, duration, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            """INSERT INTO devices (device, os, first_seen, last_seen)
               VALUES (?,?,?,?)
               ON CONFLICT(device) DO UPDATE SET
                   last_seen = excluded.last_seen,
                   os = COALESCE(excluded.os, devices.os)""",
            (device, os_name, now, now),
        )
        conn.commit()
    return len(rows)


def list_devices():
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "SELECT device, os, first_seen, last_seen FROM devices ORDER BY last_seen DESC"
        )
        return [dict(r) for r in cur.fetchall()]


def summary(device, since, until):
    """Per-app and per-domain time totals + overall stats in [since, until)."""
    conn = get_conn()
    with _lock:
        apps = conn.execute(
            """SELECT app AS name, SUM(duration) AS seconds, COUNT(*) AS hits
               FROM events
               WHERE device=? AND type='app' AND ts_start>=? AND ts_start<?
                 AND duration IS NOT NULL
               GROUP BY app ORDER BY seconds DESC""",
            (device, since, until),
        ).fetchall()
        domains = conn.execute(
            """SELECT domain AS name, SUM(duration) AS seconds, COUNT(*) AS hits
               FROM events
               WHERE device=? AND type='browser' AND ts_start>=? AND ts_start<?
                 AND duration IS NOT NULL AND domain IS NOT NULL
               GROUP BY domain ORDER BY seconds DESC""",
            (device, since, until),
        ).fetchall()
        totals = conn.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN type='app'     THEN duration END),0) AS app_seconds,
                   COALESCE(SUM(CASE WHEN type='browser' THEN duration END),0) AS web_seconds,
                   COALESCE(SUM(CASE WHEN type='afk'     THEN duration END),0) AS afk_seconds,
                   MIN(ts_start) AS first_ts,
                   MAX(COALESCE(ts_end, ts_start)) AS last_ts
               FROM events
               WHERE device=? AND ts_start>=? AND ts_start<?""",
            (device, since, until),
        ).fetchone()
    return {
        "apps": [dict(r) for r in apps],
        "domains": [dict(r) for r in domains],
        "totals": dict(totals) if totals else {},
    }


def activity(device, since, until, limit=2000):
    """Raw interval/event list, newest first."""
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            """SELECT type, app, title, url, domain, ts_start, ts_end, duration
               FROM events
               WHERE device=? AND ts_start>=? AND ts_start<?
               ORDER BY ts_start DESC LIMIT ?""",
            (device, since, until, limit),
        )
        return [dict(r) for r in cur.fetchall()]
