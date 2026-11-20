#!/usr/bin/env python3
"""A hub full of made-up activity, for trying the dashboard: two weeks of a Linux laptop's and a
Windows PC's days, heartbeat-sized events as the agents send them, a few category rules, and the
laptop still posting while it runs. Nothing in it is anyone's real data.

    python3 server/demo_server.py <empty folder> [--port 5240]

It prints the address and a sign-in. The folder keeps the data; delete it afterwards.
"""
import argparse
import importlib.util
import json
import os
import random
import secrets
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("xrnmoni_server", os.path.join(HERE, "xrnmoni.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# What each kind of stretch has in front: (app, weight, [(site, title), ...] for a browser).
LAPTOP = {
    "work": [("code", 40, None), ("Alacritty", 18, None),
             ("zen", 22, [("github.com", "Pull request #42 · example/project"), ("docs.python.org", "sqlite3 — DB-API 2.0 interface"),
                          ("developer.mozilla.org", "Content-Security-Policy - HTTP | MDN"), ("stackoverflow.com", "How to merge intervals in Python"),
                          ("mail.example.com", "Inbox (3)")]),
             ("org.telegram.desktop", 6, None), ("obsidian", 8, None), ("Slack", 6, None)],
    "browse": [("zen", 70, [("news.ycombinator.com", "Hacker News"), ("youtube.com", "Lo-fi beats to code to"),
                            ("en.wikipedia.org", "Interval tree - Wikipedia"), ("reddit.com", "r/selfhosted"),
                            ("mail.example.com", "Inbox (1)")]),
               ("org.telegram.desktop", 15, None), ("spotify", 10, None), ("Alacritty", 5, None)],
    "media": [("zen", 60, [("youtube.com", "A long video essay"), ("m.youtube.com", "Concert recording"), ("twitch.tv", "Speedrun marathon")]),
              ("mpv", 30, None), ("org.telegram.desktop", 10, None)],
}
PC = {
    "browse": [("chrome.exe", 70, [("youtube.com", "Patch notes explained"), ("reddit.com", "r/pcgaming"),
                                   ("store.steampowered.com", "Weekend deals"), ("en.wikipedia.org", "List of RPGs")]),
               ("Discord.exe", 20, None), ("explorer.exe", 10, None)],
    "game": [("factorio.exe", 75, None), ("Discord.exe", 15, None), ("steam.exe", 10, None)],
    "media": [("vlc.exe", 50, None), ("chrome.exe", 40, [("youtube.com", "Documentary"), ("netflix.com", "Netflix")]),
              ("Discord.exe", 10, None)],
}
TITLES = {
    "code": ["server.py — project", "app.js — project", "style.css — project", "README.md — project", "test_server.py — project"],
    "Alacritty": ["~/project: git status", "~/project: python3 -m unittest", "htop", "~: ssh build-box"],
    "org.telegram.desktop": ["Telegram", "Family", "Friends"],
    "obsidian": ["Daily note", "Ideas", "Reading list"],
    "Slack": ["#general", "#builds"],
    "spotify": ["Spotify Premium"],
    "mpv": ["holiday-video.mkv"],
    "factorio.exe": ["Factorio 2.0"],
    "Discord.exe": ["#game-night - Discord", "Friends - Discord"],
    "steam.exe": ["Steam", "Library"],
    "explorer.exe": ["Downloads", "This PC"],
    "vlc.exe": ["movie.mkv - VLC media player"],
}
RULES = [
    {"kind": "app", "pattern": "code", "category": "Work"},
    {"kind": "app", "pattern": "Alacritty", "category": "Work"},
    {"kind": "app", "pattern": "Slack", "category": "Work"},
    {"kind": "app", "pattern": "obsidian", "category": "Work"},
    {"kind": "site", "pattern": "github.com", "category": "Work"},
    {"kind": "site", "pattern": "docs.python.org", "category": "Work"},
    {"kind": "site", "pattern": "developer.mozilla.org", "category": "Work"},
    {"kind": "site", "pattern": "stackoverflow.com", "category": "Work"},
    {"kind": "site", "pattern": "youtube.com", "category": "Video"},
    {"kind": "site", "pattern": "twitch.tv", "category": "Video"},
    {"kind": "site", "pattern": "netflix.com", "category": "Video"},
    {"kind": "app", "pattern": "mpv", "category": "Video"},
    {"kind": "app", "pattern": "vlc.exe", "category": "Video"},
    {"kind": "app", "pattern": "factorio.exe", "category": "Games"},
    {"kind": "app", "pattern": "steam.exe", "category": "Games"},
    {"kind": "site", "pattern": "store.steampowered.com", "category": "Games"},
    {"kind": "app", "pattern": "org.telegram.desktop", "category": "Chat"},
    {"kind": "app", "pattern": "Discord.exe", "category": "Chat"},
    {"kind": "site", "pattern": "reddit.com", "category": "Social"},
    {"kind": "site", "pattern": "news.ycombinator.com", "category": "Social"},
]


def laptop_plan(weekday, rng):
    """(start hour, end hour, what) for the laptop's day."""
    j = lambda: rng.uniform(-0.25, 0.25)  # noqa: E731
    if weekday < 5:
        return [(0, 7.3 + j(), "sleep"), (7.4 + j(), 8.0, "browse"), (8.0, 10.4 + j(), "work"), (10.5, 10.7, "idle"),
                (10.7, 12.2 + j(), "work"), (12.25, 12.45, "idle"), (12.45, 13.1, "locked"), (13.1, 15.4 + j(), "work"),
                (15.45, 15.7, "idle"), (15.7, 17.8 + j(), "work"), (17.9, 19.5 + j(), "sleep"), (19.6, 21.7 + j(), "media"),
                (21.75, 22.1, "idle"), (22.15, 24, "sleep")]
    return [(0, 9.1 + j(), "sleep"), (9.2, 11.5 + j(), "browse"), (11.6, 14.0, "sleep"), (14.1, 15.8 + j(), "work"),
            (15.9, 16.2, "locked"), (16.3, 20.0, "sleep"), (20.1, 22.8 + j(), "media"), (22.9, 24, "sleep")]


def pc_plan(weekday, rng):
    j = lambda: rng.uniform(-0.3, 0.3)  # noqa: E731
    if weekday < 5 and weekday != 2:
        return [(18.3 + j(), 18.8, "browse"), (18.8, 21.4 + j(), "game"), (21.5, 21.8, "idle"), (21.8, 23.4 + j(), "media")]
    if weekday == 2:
        return [(19.0 + j(), 20.5, "browse"), (20.5, 22.0, "media")]
    return [(10.0 + j(), 11.0, "browse"), (11.0, 15.5 + j(), "game"), (15.6, 16.0, "locked"), (16.0, 18.0, "game"),
            (20.0, 22.5 + j(), "media")]


def events_for(plan, mix, day_start, until, rng):
    """The day as an agent would have sent it: focus stretches cut into heartbeat chunks of at most
    a minute, the browser's pages beside them, and boot/shutdown at the ends of a running stretch."""
    out = []

    def chunks(kind, start, end, **fields):
        t = start
        while t < end:
            e = min(end, t + rng.uniform(30, 60))
            out.append({"id": uuid.uuid4().hex, "type": kind, "ts_start": t, "ts_end": e, "duration": e - t, **fields})
            t = e

    first = True
    for a, b, what in plan:
        start, end = day_start + a * 3600, min(day_start + b * 3600, until)
        if end <= start:
            continue
        if what != "sleep" and first:
            out.append({"id": uuid.uuid4().hex, "type": "boot", "ts_start": start - 60})
        first = False
        if what in ("sleep", "idle", "locked"):
            chunks(what, start, end)
            continue
        apps = mix[what]
        t = start
        while t < end:
            app, _, sites = rng.choices(apps, weights=[w for _, w, _ in apps])[0]
            e = min(end, t + rng.expovariate(1 / (240 if what == "work" else 420)) + 15)
            if sites:
                s = t
                while s < e:
                    site, title = rng.choice(sites)
                    se = min(e, s + rng.expovariate(1 / 150) + 10)
                    chunks("app", s, se, app=app, title=f"{title} — {app}")
                    chunks("web", s, se, url=f"https://{site}/{rng.choice(['', 'a', 'b/c', 'watch?v=1'])}", title=title, domain=site)
                    s = se
            else:
                chunks("app", t, e, app=app, title=rng.choice(TITLES.get(app, [app])))
            t = e
    return out


def fill(app, now):
    rng = random.Random(7)
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(13, -1, -1):
        day = midnight - timedelta(days=back)
        start = day.timestamp()
        for device, os_name, plan, mix in (("laptop", "linux", laptop_plan, LAPTOP), ("pc", "windows", pc_plan, PC)):
            blocks = plan(day.weekday(), rng)
            if back == 0 and device == "laptop":  # at work now, whatever the hour
                cut = (now - 900 - start) / 3600
                blocks = [(a, min(b, cut), w) for a, b, w in blocks if a < cut] + [(max(cut, 0), 24, "work")]
            events = events_for(blocks, mix, start, min(now, start + 86400), rng)
            if device == "pc" and events:
                last = max(e.get("ts_end", e["ts_start"]) for e in events)
                if last < now - 300:
                    events.append({"id": uuid.uuid4().hex, "type": "shutdown", "ts_start": last + 30})
            rows = [srv.clean_event(e, now) for e in events]
            app.store.insert(device, os_name, rows, now)
    # The last post each device made: the laptop is on now, the PC was last heard of last night.
    with app.store.lock, app.store.db:
        for device in ("laptop", "pc"):
            app.store.db.execute(
                "UPDATE devices SET last_seen = (SELECT MAX(ts_end) FROM events WHERE device=?) WHERE device=?",
                (device, device))
    app.categories.write(1, RULES)


def keep_posting(port, token):
    """The laptop's agent, still at it: a chunk of the editor every few seconds."""
    rng = random.Random(1)
    last = time.time()
    while True:
        time.sleep(5)
        now = time.time()
        body = {"device": "laptop", "os": "linux", "events": [
            {"id": uuid.uuid4().hex, "type": "app", "app": "code", "title": rng.choice(TITLES["code"]),
             "ts_start": last, "ts_end": now, "duration": now - last}]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
            last = now
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("folder")
    p.add_argument("--port", type=int, default=5240)
    p.add_argument("--no-live", action="store_true", help="don't keep posting as the laptop")
    args = p.parse_args()
    data = os.path.abspath(args.folder)
    os.makedirs(data, exist_ok=True)
    if os.listdir(data):
        sys.exit(f"{data} isn't empty")
    import bcrypt

    password = secrets.token_urlsafe(9)
    with open(os.path.join(data, "users"), "w") as f:
        f.write("demo:" + bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode() + "\n")
    app = srv.App(data, legacy_token="")
    token = app.tokens.add("laptop")
    now = time.time()
    fill(app, now)
    srv.Handler.app = app
    httpd = srv.Server(("127.0.0.1", args.port), srv.Handler)
    if not args.no_live:
        threading.Thread(target=keep_posting, args=(httpd.server_address[1], token), daemon=True).start()
    print(f"http://127.0.0.1:{httpd.server_address[1]}/  sign in as demo / {password}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
