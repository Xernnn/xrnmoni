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

```bash
cd agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.toml config.toml                # then edit server_url + token + device
python agent.py --config config.toml
```

Set `server_url` to the hub's LAN IP (e.g. `http://192.168.1.10:8000`) and
`token` to the same secret. `device` is the friendly name shown in the dashboard.

**Platform notes**

- **Windows** — foreground app + idle work out of the box (pywin32).
- **Linux / X11** — active window via python-xlib. For idle/AFK detection,
  install `xprintidle` (`sudo pacman -S xprintidle` on CachyOS/Arch). Without it,
  app tracking still works but nothing is ever marked idle.
- **Linux / Hyprland (Wayland)** — active window via `hyprctl`; auto-detected.
  Idle detection isn't available on Wayland, so `afk_threshold` has no effect.
- **Other Wayland (GNOME/KDE)** — compositors don't expose the active window to
  unprivileged clients; the agent logs "unknown". Use an X11 session, or extend
  `platform_linux.py` with a GNOME-extension / KWin-script bridge.

**Run it on boot**

- Linux (systemd user service):
  ```ini
  # ~/.config/systemd/user/xrnmoni.service
  [Unit]
  Description=xrnmoni agent
  [Service]
  ExecStart=%h/Documents/Projects/xrnmoni/agent/.venv/bin/python %h/Documents/Projects/xrnmoni/agent/agent.py --config %h/Documents/Projects/xrnmoni/agent/config.toml
  Restart=always
  [Install]
  WantedBy=default.target
  ```
  `systemctl --user enable --now xrnmoni` (and `loginctl enable-linger $USER`
  so it runs without an active graphical login if you want).
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
4. Add the *Origin* the options page shows (`chrome-extension://…`) to
   `bridge_origins` in the agent's config and restart the agent. Until then the agent
   logs `[bridge] refused chrome-extension://…` once, with the origin to add.
   *Last post* on the options page shows what the agent answered (200 = recorded).

**Zen / Firefox** — the same folder works: the manifest names both a service
worker (Chromium) and an event-page script (Firefox), and has a fixed add-on id.

- Try it: `about:debugging` → *This Firefox* → *Load Temporary Add-on* → pick
  `extension/manifest.json` (gone after a restart).
- Keep it: `python scripts/package-extension.py` writes
  `dist/xrnmoni-extension-<version>.xpi` (and a `.zip` for Chromium). It is unsigned,
  so set `xpinstall.signatures.required` to `false` in `about:config` (Zen, Firefox
  Developer Edition, Nightly and ESR honour it), then drag the `.xpi` onto the
  browser. A newer version is installed the same way.
- Then set the secret and add the origin as for Chromium. Firefox's origin is
  `moz-extension://<uuid>`, different in every profile; the options page shows it.

Tests: `python -m unittest discover tests` covers the manifest and the package;
with Playwright installed, `tests/test_extension_chromium.py` also loads the
extension in headless Chromium and checks its posts reach the agent's bridge.

MV3 service workers can be suspended; a 30-second heartbeat chunks long sessions
so at most ~30s is ever lost on suspension.

---

## Data model

All events land in one `events` table:

| type       | meaning                                    | fields used                     |
|------------|--------------------------------------------|---------------------------------|
| `app`      | foreground window interval                 | app, title, ts_start/end, dur   |
| `browser`  | active-tab interval (from extension)       | url, title, domain, …, dur      |
| `afk`      | idle interval                              | ts_start/end, duration          |
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
