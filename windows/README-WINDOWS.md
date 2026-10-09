# xrnmoni — simple setup for Windows 11 (non-technical guide)

This sets up free monitoring on your son's Windows 11 PC that you can check from
your phone or laptop anywhere — no monthly fee, replacing your paid app.

Everything runs **on his PC**. You view it over a free private link (Tailscale).
Do each part in order. It takes about 20 minutes once.

---

## Part 1 — Put the files on his PC

Copy the whole `xrnmoni` folder onto his PC (USB stick is easiest). For example
put it at: `C:\xrnmoni`

## Part 2 — Install Tailscale (the free private link)

Tailscale is what lets you see his PC from your phone/laptop even on your VPN.

1. On **his PC**: go to https://tailscale.com/download, install it, and sign in
   (use a free Google/Microsoft account — remember which one).
2. On **your phone or laptop**: install Tailscale too and sign in with the
   **same** account.
3. That's it — the two devices can now reach each other privately.

> If you use another VPN (Nord, Proton, etc.) on your viewing device, you may
> need to turn it off for a minute when checking the dashboard, or they can
> conflict. His PC only needs Tailscale.

## Part 3 — Run the installer (one time, on his PC)

1. Click the **Start** button, type **Terminal**, right-click it, choose
   **Run as administrator**, click **Yes**.
2. Copy-paste this line and press Enter (fix the path if you didn't use C:\xrnmoni):

   ```
   powershell -ExecutionPolicy Bypass -File "C:\xrnmoni\windows\install.ps1"
   ```

3. When it asks for a password/token, type something you'll remember (or just
   press Enter and it makes one for you). **Write it down** — you'll need it to
   log in to the dashboard.
4. When it finishes it prints a web address like `http://100.x.x.x:8000` and
   your token. Note both.

From now on it starts by itself every time he logs on. You don't touch his PC again.

## Part 4 — Turn on website tracking (his web browser)

To see websites, add the little browser helper **in whatever browser he uses**:

1. In Chrome/Edge, go to `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right switch).
3. Click **Load unpacked** and pick the `C:\xrnmoni\extension` folder.
4. Click **Details** on it and turn on **Allow in Incognito / InPrivate** — this
   is what lets you see private-mode browsing too.

Repeat for each browser he uses.

## Part 5 — Check it from your phone or laptop

1. Make sure Tailscale is **on** (and your other VPN off if it fights it).
2. Open a web browser and go to the address from Part 3, e.g. `http://100.x.x.x:8000`
3. Type your token/password in the box (top-right). Done — you'll see his
   activity, updating live.

Tip: on your phone you can "Add to Home Screen" so it opens like an app.

---

## Everyday use

- Pick the day with the date box; pick his PC in the dropdown.
- Cards show total screen time, time on the web, and idle time.
- Bars show his top apps and top websites for that day.
- The timeline lists everything, newest first.
- The dot next to his PC's name is green when it's currently on.

## If something's wrong

- **Dashboard won't load:** is his PC on? Is Tailscale on (both devices)? Is your
  other VPN interfering — try turning it off.
- **No websites showing:** the browser helper (Part 4) isn't added in that browser.
- **Start it by hand:** double-click `windows\start-xrnmoni.bat` on his PC.
- **Stop it:** double-click `windows\stop-xrnmoni.bat`.
- **Remove it completely:** double-click `windows\uninstall.bat`.
