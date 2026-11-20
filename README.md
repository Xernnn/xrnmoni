<p align="center">
  <img src="extension/icons/icon128.png" width="72" height="72" alt="">
</p>

<h1 align="center">xrnmoni</h1>

Self-hosted activity monitor for **machines you own**. Each machine runs a small
agent that records which app is in the foreground (and for how long), idle time,
boot/shutdown, and — via a browser extension — which web pages are open in real
time. Everything streams to one central server with a web dashboard you open
from anywhere on your network.

> **Scope / consent.** This is built for monitoring your own devices, or devices
> whose users know and consent (the parental-controls model). Don't deploy it
> covertly on another adult's machine — that's stalkerware and in many places
> illegal. There is intentionally no stealth/hide mode.

```
Windows PC ─┐                         ┌─ you (one browser tab)
            ├─ agent + extension ──┐  │
Linux laptop┘                      ▼  ▼
                            server (FastAPI + SQLite) + dashboard
```

## Layout

| Path          | What it is                                            |
|---------------|-------------------------------------------------------|
| `server/`     | FastAPI ingest + query API, SQLite store, dashboard   |
| `agent/`      | Cross-platform agent (Windows + Linux)                |
| `extension/`  | MV3 browser extension for live page tracking          |

---

## 1. Server (run once, on whichever machine is your "hub")

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
XRNMONI_TOKEN="pick-a-long-secret" uvicorn main:app --host 0.0.0.0 --port 8000
```

- Dashboard: `http://<server-ip>:8000/` — paste the same token into the token box.
- The DB file `server/xrnmoni.db` is created automatically. Override its path
  with `XRNMONI_DB=/path/to.db`.
- On Windows PowerShell set the env var with `$env:XRNMONI_TOKEN="..."` first.

The server binds to all interfaces so your other machines can reach it. Keep it
on a trusted LAN, or tunnel it (Tailscale / WireGuard / SSH) rather than exposing
port 8000 to the internet. The token is the only access control, and it travels in
plain HTTP, so only send it over a network you trust or one that encrypts (Tailscale
does). The server won't start without `XRNMONI_TOKEN`, or with `change-me`; use 16 or
more random characters (`python -c "import secrets; print(secrets.token_urlsafe(24))"`).

## Security

- The dashboard escapes everything agents send (window and page titles are chosen by
  whatever app or website was open) and runs under a Content-Security-Policy that
  allows only its own script and requests to this server.
- The agent's browser bridge (`127.0.0.1:8129`) accepts posts only from the extension
  origins listed in `bridge_origins` that send the shared `bridge_secret`, so neither
  websites you visit, other extensions nor other local programs can feed it made-up
  activity. It checks every field and answers 400 to anything malformed. Page addresses
  are kept without their `?query` and `#fragment` unless `keep_url_query = true`.
- Tests: `pip install -r server/requirements.txt -r agent/requirements.txt httpx`,
  then `python -m unittest discover tests`.

## 2. Agent (on each monitored machine)

**Linux** (systemd user service):

```bash
linux/install.sh
```

It makes a venv (`agent/.venv`), copies the example config to
`~/.config/xrnmoni/agent.toml` if there is none (with a fresh `bridge_secret`, which it
prints for the extension), and installs and enables the user unit
`xrnmoni-agent.service`. The unit starts with the graphical session and stops with it
(`WantedBy`/`PartOf=graphical-session.target`), so Hyprland's and Wayland's sockets are
there when it starts; it restarts on a crash. Edit the config, then
`systemctl --user start xrnmoni-agent`; the log is
`journalctl --user -u xrnmoni-agent -f`. Run `install.sh` again after pulling to
update; it keeps the config and restarts the agent.

**By hand** (any platform):

```bash
cd agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.toml config.toml                # then edit server_url + token + device
python agent.py --config config.toml
```

Set `server_url` to the hub's LAN IP (e.g. `http://192.168.1.10:8000`), or a URL with
a path if the server sits behind a proxy (`https://host/xrnmoni`), and `token` to the
same secret. `device` is the friendly name shown in the dashboard.

**What it records, and how it survives trouble**

- The foreground window (`app`), idle time (`idle`: no input for `afk_threshold`
  seconds), the locked screen (`locked`) and sleep (`sleep`). One at a time: sleep
  beats locked, locked beats idle, idle beats the window.
- Intervals are sent as closed chunks of at most 60 s while they last, and split at
  local midnight, so a long stay shows up within a minute and day totals add up.
- Every event goes into a SQLite spool (`spool_path`, default
  `~/.local/state/xrnmoni/spool.sqlite`, `%LOCALAPPDATA%\xrnmoni\` on Windows) when it
  is made, and is deleted only after the server took it. A server outage, a restart or
  a crash loses nothing but the current minute; SIGTERM (systemd stop), Ctrl-C and
  SIGHUP close the open interval first. Each event has an id, so a resend is not
  counted twice. Events the server refuses are logged and dropped; the spool is
  capped by `spool_max_days` / `spool_max_rows`, oldest first.
- A suspend (or the agent being stopped) is noticed when the loop wakes to more wall
  time than it slept: the gap is recorded as `sleep`, not as time in the last window.

**Platform notes**

- **Windows** — foreground app + idle work out of the box (pywin32).
- **Linux / Hyprland (Wayland)** — the focused window comes from Hyprland's event
  socket, so even short switches are seen; auto-detected, also when the agent started
  before Hyprland. Idle comes from the Wayland idle protocol (ext-idle-notify): the
  agent asks the compositor to tell it after `afk_threshold` seconds without input. It
  only listens and never keeps the screen awake; a video that keeps the screen on
  counts as being there. The lock screen is seen through logind and Hyprland's
  lock-notify protocol (hyprlock, the Omarchy/Quickshell lock, swaylock), and sleep
  through logind. The log's first lines say which sources are in use.
- **Linux / X11** — active window via python-xlib. For idle detection, install
  `xprintidle` (`sudo pacman -S xprintidle` on CachyOS/Arch); without it the agent
  falls back to logind's idle hint, which many desktops never set.
- **Other Wayland (GNOME/KDE)** — compositors don't expose the active window to
  unprivileged clients; the agent logs "unknown". Use an X11 session, or extend
  `platform_linux.py` with a GNOME-extension / KWin-script bridge. Idle works where
  the compositor has ext-idle-notify (KDE does).

**Run it on boot**

- Linux: `linux/install.sh`, above.
- Windows: Task Scheduler → "At log on" → run `pythonw.exe agent.py`.

## 3. Browser extension (for live website tracking)

The extension reports the active tab's URL/title/time to the agent on
`127.0.0.1:<bridge_port>` — so the agent must be running on that machine. It
never holds the server URL or token.

**Chrome / Edge / Brave**

1. `chrome://extensions` → enable *Developer mode*.
2. *Load unpacked* → select the `extension/` folder.
3. Open the extension's options: set *Secret* to `bridge_secret` from the agent's
   config (and *Port* if you changed `bridge_port`), then *Save*.
4. The manifest's public `key` gives the extension the same id everywhere,
   `jiogollbnlahboaadnimlllpohpmgnaa`, and the example config already lists
   `chrome-extension://jiogollbnlahboaadnimlllpohpmgnaa` in `bridge_origins`. An origin
   that isn't listed is refused, and the agent logs `[bridge] refused …` once with the
   origin to add. *Last post* on the options page shows what the agent answered
   (200 = recorded).

**Zen / Firefox** — the same folder works: the manifest names both a service
worker (Chromium) and an event-page script (Firefox), and has a fixed add-on id.

- Try it: `about:debugging` → *This Firefox* → *Load Temporary Add-on* → pick
  `extension/manifest.json` (gone after a restart).
- Keep it: `python scripts/package-extension.py` writes
  `dist/xrnmoni-extension-<version>.xpi` (and a `.zip` for Chromium). It is unsigned,
  so set `xpinstall.signatures.required` to `false` in `about:config` (Zen, Firefox
  Developer Edition, Nightly and ESR honour it), then drag the `.xpi` onto the
  browser. A newer version is installed the same way.
- Then set the secret as for Chromium, and add the origin to `bridge_origins`:
  Firefox's is `moz-extension://<uuid>`, different in every profile (the options
  page shows it). The add-on id is `xrnmoni@xrnmoni`.

Tests: `python -m unittest discover tests` covers the manifest and the package;
with Playwright installed, `tests/test_extension_chromium.py` also loads the
extension in headless Chromium and checks its posts reach the agent's bridge.

MV3 service workers can be suspended; a 30-second heartbeat chunks long sessions
so at most ~30s is ever lost on suspension.

---

## Data model

All events land in one `events` table. Each carries an `id` (32 hex characters)
that the agent gives it, and intervals are at most 60 s long:

| type       | meaning                                    | fields used                     |
|------------|--------------------------------------------|---------------------------------|
| `app`      | foreground window interval                 | app, title, ts_start/end, dur   |
| `browser`  | active-tab interval (from extension)       | url, title, domain, …, dur      |
| `idle`     | no input for `afk_threshold` s (was `afk`) | ts_start/end, duration          |
| `locked`   | screen locked                              | ts_start/end, duration          |
| `sleep`    | machine asleep, or the agent not running   | ts_start/end, duration          |
| `boot`     | machine boot time (point event)            | ts_start                        |
| `shutdown` | agent stopping (point event)               | ts_start                        |

## API (all require `Authorization: Bearer <token>`)

- `POST /api/events` — `{device, os, events:[…]}` (agents)
- `GET  /api/devices`
- `GET  /api/summary?device=&since=&until=`  (epoch seconds)
- `GET  /api/activity?device=&since=&until=`

## Roadmap / easy extensions

- Screenshots on an interval (add a `screenshot` event type + static file store).
- Category rules (map apps/domains → "work" / "social" / "games").
- Alerts (notify when a device uses X for more than N minutes).
- Per-device auth tokens instead of one shared token.
