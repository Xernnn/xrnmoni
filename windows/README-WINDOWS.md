# xrnmoni on Windows 10 and 11

The agent records which app is in front on this PC, and for how long, idle and locked
time, and (with the browser extension) which web pages are open. It sends that to your
hub, which normally runs on another machine and is reached over HTTPS, often under a path
such as `https://hub.example.net/xrnmoni`. It starts by itself at every logon, shows no
window, and runs as the signed-in user. Installing it needs no administrator.

Use it on your own PC, or one whose user knows it is there (see the scope note in the
main [README](../README.md)).

---

## 1. Before you start

- **A device token for this PC.** On the hub:
  `python3 server/xrnmoni.py token add den-pc --data <hub data folder>` prints one.
  It belongs to that device name, so use the same name below.
- **The hub's address**, with its path if it has one: `https://hub.example.net/xrnmoni`.
- **The files**, any of:
  - the **Windows** download on the hub's Get page (`<hub>/get`, opened with the device
    token), or the `xrnmoni-windows-<version>.zip` of a release: the package below;
  - this repository's folder (the installer finds Python 3.10+, or installs it for the
    user with winget), or
  - the **`xrnmoni-windows`** package, from the Artifacts of a CI run (Actions, the latest
    run on `main`, `windows` job). It holds `xrnmoni-agent.exe` with Python built in, so
    the PC needs no Python. To build it yourself on a Windows machine with Python:
    `powershell -ExecutionPolicy Bypass -File windows\build-exe.ps1`
    (it ends up in `windows\dist\xrnmoni-windows`).

Copy the folder anywhere on the PC (Downloads is fine): the installer copies what it needs.

## 2. Install

Sign in to Windows **as the person whose activity is recorded**, then double-click
`windows\install.bat` (in the package too: there it installs the exe). Or in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File windows\install.ps1
```

It asks for the hub's address, the device token (not shown while you type or paste it)
and the PC's name, then:

1. finds Python, or installs Python 3.13 for this user with winget (not with `-UseExe`);
2. copies the agent to `%LOCALAPPDATA%\xrnmoni\app` with a venv beside it (or the exe to
   `...\xrnmoni\bin`), and the extension to `...\xrnmoni\extension`;
3. writes `%APPDATA%\xrnmoni\config.toml`, with a new secret for the browser extension;
4. registers the scheduled task **xrnmoni agent**: at this user's logon, as this user,
   only while they are signed in, not elevated, `pythonw.exe` (or the exe) so there is no
   window, no time limit, kept running on battery, and started again within 5 minutes if
   it stops;
5. starts it, and prints the secret to give the extension.

Every question can be answered on the command line instead, for example:

```powershell
.\windows\install.ps1 -HubUrl https://hub.example.net/xrnmoni -DeviceToken xrnmoni_... -DeviceName den-pc
```

| Option | What it does |
|---|---|
| `-HubUrl`, `-DeviceToken`, `-DeviceName` | the answers to the three questions |
| `-Reconfigure` | ask all three again (otherwise a second run keeps the settings) |
| `-BridgeOrigin <origin>` | let one more browser's extension post (see 3) |
| `-UseExe`, `-ExePath <exe>` | install `xrnmoni-agent.exe` instead of Python and a venv |
| `-Python <python.exe>` | use this Python |
| `-NoStart` | don't start it now (it starts at the next logon) |
| `-WithHub`, `-HubBind`, `-HubPort`, `-HubArgs` | also run a hub on this PC (see 8) |

Several people use the PC? Run the installer once while signed in as each of them; each
gets their own task, config and device name.

The exe isn't signed, so SmartScreen may say "Windows protected your PC" when you first
run a script from a downloaded folder: **More info**, **Run anyway**. If antivirus
quarantines `xrnmoni-agent.exe`, restore it and allow
`%LOCALAPPDATA%\xrnmoni\bin`, or use the Python install instead.

## 3. Browser extension (website time)

The extension tells the agent, on `127.0.0.1` only, which page is open. The agent takes
its reports only with the secret from `config.toml` (`bridge_secret`) and only from the
extension origins listed there (`bridge_origins`).

**Chrome, Edge, Brave**

1. Open `chrome://extensions` (Edge: `edge://extensions`) and turn on **Developer mode**.
2. **Load unpacked**, and pick `%LOCALAPPDATA%\xrnmoni\extension` (paste that into the
   folder box's address bar). The installer keeps this folder up to date. The extension's
   id is the same in every Chromium browser and on every PC (its manifest has a public
   key), `jiogollbnlahboaadnimlllpohpmgnaa`, and the installer has already listed its
   origin.
3. **Details**: turn on **Allow in Incognito** (Edge: **Allow in InPrivate**), then open
   **Extension options**.
4. Paste the secret the installer printed into **Secret** (it is also `bridge_secret` in
   `%APPDATA%\xrnmoni\config.toml`), leave **Port** at 8129 unless you changed
   `bridge_port`, and **Save**.
5. Browse for a minute. **Last post** on the options page should say `200`.

**Origin** on the options page is `chrome-extension://jiogollbnlahboaadnimlllpohpmgnaa`;
it is in `bridge_origins` already. Chrome may ask at startup about developer-mode
extensions; keep it.

**Firefox**

Release Firefox runs only signed extensions for good, so pick one:

- **For a try:** `about:debugging`, **This Firefox**, **Load Temporary Add-on**, and pick
  `%LOCALAPPDATA%\xrnmoni\extension\manifest.json`. It is gone when Firefox closes.
- **To keep it:** Firefox Developer Edition, Nightly or ESR, with
  `xpinstall.signatures.required` set to `false` in `about:config`, then install the
  extension packed as an `.xpi` (a zip of the extension folder's contents). Or sign it
  for yourself, unlisted, on addons.mozilla.org, and install that in any Firefox.

Then in `about:addons`, xrnmoni: **Run in Private Windows: Allow**, and its **Options**:
paste the secret, **Save**, and copy the **Origin** (`moz-extension://...`, different in
every Firefox profile) into `windows\install.bat -BridgeOrigin moz-extension://...`.

## 3a. Private windows off (a child's PC)

A private window is where xrnmoni records only "Private" and its time. On a child's PC you can
close them in every Chromium browser at once, for every account, as machine policy that a
standard (not administrator) account can't change. In an **administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File windows\admin\private-windows.ps1
```

It turns off Chrome's and Chromium's Incognito, Edge's InPrivate, and Brave's private and Tor
windows; each browser takes it at its next start, and `chrome://policy` (`edge://policy`)
lists it. `-Status` shows each value, `-Undo` allows them again. Give the child a standard
account, or they can undo it.

## 4. Every day

Nothing to do. `windows\status.bat` shows whether it runs and reaches the hub.
`windows\stop-xrnmoni.bat` stops it until `windows\start-xrnmoni.bat` (also across
logons); the scripts are in `%LOCALAPPDATA%\xrnmoni\app\windows` too.

While the hub can't be reached, events wait in `%LOCALAPPDATA%\xrnmoni\spool.sqlite`
(up to 30 days) and are sent when it can. Locking the PC, being idle and sleep are
recorded as such, not as time in the last app. Store apps (Calculator, Settings) are named
after their own program, not `ApplicationFrameHost.exe`; a window running as
administrator is named after its program too, or `elevated` if Windows won't say.

## 5. Update

Copy the newer folder (or package) to the PC and run `windows\install.bat` again. It stops the agent politely, replaces the program, keeps the settings and
unsent events, and starts it again. Then reload the extension: `chrome://extensions`,
xrnmoni, the reload arrow (Firefox: reload it in `about:debugging`).

## 6. Uninstall

1. Remove the extension from each browser.
2. Double-click `windows\uninstall.bat`. It stops xrnmoni, removes its task, program,
   venv, exe and extension folder, and keeps the settings, logs and unsent events.
3. To remove those too:
   `powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1 -RemoveData`.

## 7. Files and logs

| Where | What |
|---|---|
| `%APPDATA%\xrnmoni\config.toml` | hub address, device token and name, extension secret and origins; other settings from `agent\config.example.toml` can be added below them and are kept |
| `%LOCALAPPDATA%\xrnmoni\logs\agent.log` | what the agent did and why it failed; 1 MB, five older files `agent.log.1` ... `.5` |
| `%LOCALAPPDATA%\xrnmoni\logs\agent-crash.log` | a trace if Python itself crashed |
| `%LOCALAPPDATA%\xrnmoni\spool.sqlite` | events not yet taken by the hub |
| `%LOCALAPPDATA%\xrnmoni\app`, `venv`, `bin`, `extension` | the program |
| `%LOCALAPPDATA%\xrnmoni\agent.pid` | the running agent's process id |
| `%LOCALAPPDATA%\xrnmoni\hub\` | with `-WithHub`: the hub's data and `xrnmoni.log` |

To watch the log: `Get-Content $env:LOCALAPPDATA\xrnmoni\logs\agent.log -Tail 30`.

## 8. A hub on this PC instead (`-WithHub`)

`install.ps1 -WithHub` also installs the hub (`server\xrnmoni.py`, Python needed) as the
task **xrnmoni hub**, listening on `127.0.0.1:5240` only, makes this PC's device token
itself and points the agent at it. Make a dashboard sign-in once with the command it
prints (`... xrnmoni.py user add <name> --data ...`), then open `http://127.0.0.1:5240/`.

For other machines to reach it, `-HubBind <this PC's LAN or Tailscale address>`. Windows
Firewall then asks an administrator once; allow private networks only. The hub speaks
plain HTTP: use it over Tailscale or behind an HTTPS proxy. Signing in to its dashboard
works over plain HTTP from another machine as it is (the sign-in cookie isn't marked
Secure then). `-HubArgs` passes more of the hub's options, such as `'--viewer sam'`.

## 9. If something is wrong

Run `windows\status.bat` first. It shows the task, the processes, the settings, whether
the hub answers, whether the extension's port is open, and the end of the log.

**It isn't running**

- *Task "Disabled"*: `stop-xrnmoni.bat` was used. Run `start-xrnmoni.bat`.
- *No task at all*: run the installer again, signed in as that user. On PCs where users
  may not make tasks, the installer uses a shortcut in the Startup folder instead; that
  starts it at logon but doesn't restart it.
- *Result `0x800710E0`*: the task was started while the user wasn't signed in; it starts
  at their next logon. *`0x41301`* means running; *`0x1`* or another number means it
  stopped with an error, which is at the end of `agent.log`.
- *`agent.log` says "another xrnmoni agent is already running in this session"*: it is
  running; see the processes in status.
- *The log ends with an error about `config.toml`*: run the installer with `-Reconfigure`.
- *With `-UseExe` it never starts*: antivirus may have quarantined the exe (see 2).

**The hub isn't reached**

- *status says "hub answers: no"*: open `<hub address>/healthz` in a browser on this PC;
  it should show `{"ok":true}`. If not, the address (with its path) is wrong, or the PC
  can't reach the hub (VPN, firewall, the hub is down). Fix the address with
  `install.bat -Reconfigure`.
- *`agent.log` shows 401 or 403 from the server*: the token is wrong, revoked, or was made
  for another device name. Make a new one on the hub and run
  `install.bat -DeviceToken <token> -DeviceName <the name it was made for>`.
- *`agent.log` shows certificate (SSL) errors while the browser opens the hub fine*: an
  antivirus that inspects HTTPS is in the way; turn its HTTPS scanning off for
  `pythonw.exe` / `xrnmoni-agent.exe`.
- Nothing is lost meanwhile: events wait in `spool.sqlite`.

**Websites aren't recorded**

- The options page's **Last post** says what happened to the last report:
  - *no secret set*: paste the secret (`bridge_secret` in `config.toml`) and Save;
  - *agent offline*: the agent isn't running, or **Port** isn't its `bridge_port`;
  - *403*: this browser's origin isn't in `bridge_origins`, or the secret is wrong;
    `agent.log` says which ("refused ..." or "sent no or the wrong X-Xrnmoni-Secret").
    Add the origin with `install.bat -BridgeOrigin <origin>`.
- status says *nothing listens on 127.0.0.1:8129*: the agent isn't running, or another
  program has that port; set another `bridge_port` in `config.toml`, the same port on the
  options page, and run `start-xrnmoni.bat` after `stop-xrnmoni.bat`.
- Private windows are recorded only with **Allow in Incognito / InPrivate / Private
  Windows** turned on.

**Moving from the first installer** (the one that needed "Run as administrator"): run the
new one as the user to record. It removes the old elevated task if Windows lets it, and
otherwise prints the command to run in an administrator terminal
(`schtasks /delete /tn xrnmoni /f`), along with the one for the old firewall rule.
