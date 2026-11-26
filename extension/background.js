// xrnmoni web tracker — reports active-tab page intervals to the local agent.
//
// It posts to 127.0.0.1 only; the agent tags the events with this machine's
// name and forwards them to the central server. The extension never holds the
// server URL or token.
//
// The options page sets the agent's port (bridge_port) and the shared secret
// (bridge_secret), sent as X-Xrnmoni-Secret; the agent takes posts only from the
// origins in its bridge_origins that send it.

const DEFAULTS = { bridgePort: 8129, bridgeSecret: "" };
// A private (incognito) window's tab is sent as its time and this, never its address or title.
const PRIVATE = "Private";
const IDLE_SECONDS = 60;      // match the agent's afk_threshold-ish
const HEARTBEAT_MIN = 0.5;    // 30s: chunk long sessions + keep SW warm

// In-memory current interval; mirrored to session storage so a service-worker
// restart loses at most one heartbeat chunk.
let current = null; // { url, title, domain, private, start }

function domainOf(url) {
  try { return new URL(url).hostname || null; } catch { return null; }
}

async function saveCurrent() {
  try { await chrome.storage.session.set({ current }); } catch {}
}

async function settings() {
  try { return await chrome.storage.local.get(DEFAULTS); } catch { return DEFAULTS; }
}

// What the last post got, for the options page.
async function note(status) {
  try { await chrome.storage.local.set({ lastPost: { at: Date.now(), status } }); } catch {}
}

async function post(interval) {
  const { bridgePort, bridgeSecret } = await settings();
  if (!bridgeSecret) { await note("no secret set"); return; }
  try {
    const r = await fetch(`http://127.0.0.1:${bridgePort}/browser`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Xrnmoni-Secret": bridgeSecret },
      body: JSON.stringify(interval),
    });
    await note(String(r.status));
  } catch (e) {
    // agent offline — drop it; the agent is the buffering layer, not us
    await note("agent offline");
  }
}

function now() { return Date.now() / 1000; }

// Each interval gets an id once, so the agent and the server can tell a resend
// from a new interval.
function newId() { return crypto.randomUUID().replaceAll("-", ""); }

// What is sent for one stretch of a page.
function interval(page, startTs, endTs) {
  const out = { id: newId(), url: page.url, title: page.title, domain: page.domain };
  if (page.private) out.private = true;
  return Object.assign(out, { ts_start: startTs, ts_end: endTs, duration: endTs - startTs });
}

async function stop(endTs) {
  if (current && endTs > current.start) {
    await post(interval(current, current.start, endTs));
  }
  current = null;
  await saveCurrent();
}

async function start(page, startTs) {
  current = Object.assign({}, page, { start: startTs });
  await saveCurrent();
}

// The tab as it is recorded: a private window's tabs are all one "Private" page.
function pageOf(tab) {
  if (tab.incognito) return { url: null, title: PRIVATE, domain: null, private: true };
  return { url: tab.url, title: tab.title || "", domain: domainOf(tab.url), private: false };
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

  if (!tab || !(tab.url || tab.incognito)) { await stop(now()); return; }

  const page = pageOf(tab);
  const t = now();
  if (!current || current.url !== page.url || !current.private !== !page.private) {
    await stop(t);
    await start(page, t);
  } else if (current.title !== page.title) {
    current.title = page.title; // same page, title updated (e.g. SPA)
    await saveCurrent();
  }
}

// Heartbeat: finalize the current chunk and immediately continue it, so long
// sessions are recorded incrementally and survive SW termination.
async function heartbeat() {
  if (current) {
    const t = now();
    if (t > current.start) {
      await post(interval(current, current.start, t));
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
