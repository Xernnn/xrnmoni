// xrnmoni web tracker — reports active-tab page intervals to the local agent.
//
// It posts to 127.0.0.1 only; the agent tags the events with this machine's
// name and forwards them to the central server. The extension never holds the
// server URL or token.
//
// If you changed `bridge_port` in the agent config, change BRIDGE_PORT below.

const BRIDGE_PORT = 8129;
const BRIDGE_URL = `http://127.0.0.1:${BRIDGE_PORT}/browser`;
const IDLE_SECONDS = 60;      // match the agent's afk_threshold-ish
const HEARTBEAT_MIN = 0.5;    // 30s: chunk long sessions + keep SW warm

// In-memory current interval; mirrored to session storage so a service-worker
// restart loses at most one heartbeat chunk.
let current = null; // { url, title, domain, start }

function domainOf(url) {
  try { return new URL(url).hostname || null; } catch { return null; }
}

async function saveCurrent() {
  try { await chrome.storage.session.set({ current }); } catch {}
}

async function post(interval) {
  try {
    await fetch(BRIDGE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(interval),
    });
  } catch (e) {
    // agent offline — drop it; the agent is the buffering layer, not us
  }
}

function now() { return Date.now() / 1000; }

async function stop(endTs) {
  if (current && endTs > current.start) {
    await post({
      url: current.url,
      title: current.title,
      domain: current.domain,
      ts_start: current.start,
      ts_end: endTs,
      duration: endTs - current.start,
    });
  }
  current = null;
  await saveCurrent();
}

async function start(url, title, startTs) {
  current = { url, title, domain: domainOf(url), start: startTs };
  await saveCurrent();
}

// Re-evaluate what the user is actually looking at right now.
async function reeval() {
  let tab;
  try {
    const wins = await chrome.windows.getLastFocused({ populate: false });
    if (!wins || wins.focused === false) { await stop(now()); return; }
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    tab = tabs && tabs[0];
  } catch { return; }

  if (!tab || !tab.url) { await stop(now()); return; }

  const url = tab.url;
  const title = tab.title || "";
  const t = now();
  if (!current || current.url !== url) {
    await stop(t);
    await start(url, title, t);
  } else if (current.title !== title) {
    current.title = title; // same page, title updated (e.g. SPA)
    await saveCurrent();
  }
}

// Heartbeat: finalize the current chunk and immediately continue it, so long
// sessions are recorded incrementally and survive SW termination.
async function heartbeat() {
  if (current) {
    const t = now();
    const url = current.url, title = current.title, domain = current.domain, start = current.start;
    if (t > start) {
      await post({ url, title, domain, ts_start: start, ts_end: t, duration: t - start });
      current.start = t;
      await saveCurrent();
    }
  }
}

// ---- events ----------------------------------------------------------------
chrome.tabs.onActivated.addListener(reeval);
chrome.tabs.onUpdated.addListener((id, info, tab) => {
  if ((info.url || info.status === "complete") && tab.active) reeval();
});
chrome.windows.onFocusChanged.addListener(reeval);
chrome.idle.setDetectionInterval(IDLE_SECONDS);
chrome.idle.onStateChanged.addListener(async (state) => {
  if (state === "active") reeval();
  else await stop(now());   // idle or locked
});

chrome.runtime.onStartup.addListener(reeval);
chrome.runtime.onInstalled.addListener(reeval);

chrome.alarms.create("heartbeat", { periodInMinutes: HEARTBEAT_MIN });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "heartbeat") heartbeat(); });

// Recover mirrored state when the service worker respawns.
chrome.storage.session.get("current").then((d) => {
  if (d && d.current) current = d.current;
  reeval();
}).catch(reeval);
