# xrnmoni

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
port 8000 to the internet. The token is the only access control.

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
3. If you changed `bridge_port` in the agent config, update `BRIDGE_PORT` in
   `extension/background.js` to match, then reload the extension.

**Firefox** — set `"background": { "scripts": ["background.js"] }` in the
manifest (MV3 event pages) and load it via `about:debugging` → *This Firefox* →
*Load Temporary Add-on*. `chrome.*` APIs are available as `browser.*`/`chrome.*`.

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
