// The dashboard: each device's day, the week, and the category rules.
//
// Everything shown comes from agents, and window and page titles are chosen by whatever app or
// website was open, so text only ever goes in through textContent, never as HTML.
import { dayBounds, hourMarks, parseKey, shiftKey, today, weekBounds, weekDays } from "./dates.js";

const CLIENT = { "X-Xrn-Client": "xrnmoni" };
const AWAY = { idle: "Idle", locked: "Locked", sleep: "Asleep" };
const KNOWN = new Set(["app", "web", "idle", "locked", "sleep"]);
const ONLINE_S = 120;
const SLOTS = 8;
const SHOWN = 8;

const $app = document.getElementById("app");
const $tip = document.getElementById("tip");

let state = readHash();
let devices = [];
let skew = 0; // the server's clock minus this one, in seconds
let seq = 0;
let signedIn = false;
let watching = false;
let renderCount = 0;
let refreshTimer = 0;
let $main = null;
let $sheet = null;
let notice = ""; // said once, on the next view drawn

class Unauthorized extends Error {}

// ---- small helpers -----------------------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

const local = {
  get(key) {
    try {
      return JSON.parse(localStorage.getItem(key));
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // private window: nothing is remembered
    }
  },
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const nowS = () => Date.now() / 1000 + skew;

function dur(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function hours(s) {
  if (s < 3600) return dur(s);
  const h = s / 3600;
  return `${Number.isInteger(h) || h >= 10 ? Math.round(h) : h.toFixed(1)}h`;
}

// 24-hour, as the timeline's hours are.
function clock(ts) {
  return ts == null ? "—" : new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function icon(...paths) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  for (const [d, filled] of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    if (filled) path.setAttribute("class", "fill");
    svg.append(path);
  }
  return svg;
}

function ago(s) {
  s = Math.max(0, s);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function dayLabel(key, opts = { weekday: "short", day: "numeric", month: "short" }) {
  return parseKey(key).toLocaleDateString([], opts);
}

function sum(rows) {
  return rows.reduce((a, r) => a + r[1], 0);
}

// Rows of [name, seconds, ...] from several devices, added up by name.
function merge(lists) {
  const by = new Map();
  for (const rows of lists) {
    for (const [name, secs, ...rest] of rows) {
      const cur = by.get(name);
      if (cur) cur[1] += secs;
      else by.set(name, [name, secs, ...rest]);
    }
  }
  return [...by.values()].sort((a, b) => b[1] - a[1]);
}

// A colour stays with its app (or device, or category) from one day and view to the next, while
// there are slots: the names on screen keep theirs, and a new one takes a free one.
function colours(kind, names) {
  const key = `xrnmoni.colours.${kind}`;
  const kept = local.get(key) || {};
  const out = new Map();
  const used = new Set();
  const top = names.slice(0, SLOTS);
  for (const n of top) {
    const slot = kept[n];
    if (slot && !used.has(slot)) {
      out.set(n, slot);
      used.add(slot);
    }
  }
  for (const n of top) {
    if (out.has(n)) continue;
    let slot = 1;
    while (used.has(slot)) slot += 1;
    out.set(n, slot);
    used.add(slot);
    delete kept[n];
    kept[n] = slot;
  }
  const names64 = Object.keys(kept);
  for (const n of names64.slice(0, Math.max(0, names64.length - 64))) delete kept[n];
  local.set(key, kept);
  return new Map([...out].map(([n, slot]) => [n, `var(--c${slot})`]));
}

// ---- the server --------------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts, headers: { ...CLIENT, ...(opts.headers || {}) } });
  if (res.status === 401) {
    showSignIn();
    throw new Unauthorized();
  }
  if (!res.ok && res.status !== 304) throw new Error(String(res.status));
  return res;
}

const getJSON = async (path) => (await api(path)).json();

function deviceParam(device = state.device) {
  return device ? `&device=${encodeURIComponent(device)}` : "";
}

async function loadDevices() {
  const doc = await getJSON("api/devices");
  devices = doc.devices;
  skew = doc.now - Date.now() / 1000;
  if (!seq) seq = doc.seq;
  return devices;
}

// ---- where we are ------------------------------------------------------------------------------

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const view = ["day", "week", "rules"].includes(p.get("v")) ? p.get("v") : "day";
  const date = parseKey(p.get("d")) ? p.get("d") : today();
  return { view, date, device: p.get("dev") || "", colour: p.get("c") === "cat" ? "cat" : "app" };
}

function go(change) {
  const next = { ...state, ...change };
  const p = new URLSearchParams();
  if (next.view !== "day") p.set("v", next.view);
  if (next.date !== today()) p.set("d", next.date); // without one it follows today past midnight
  if (next.device) p.set("dev", next.device);
  if (next.colour === "cat") p.set("c", "cat");
  const hash = p.toString();
  if (hash === location.hash.slice(1)) render();
  else location.hash = hash;
}

window.addEventListener("hashchange", () => {
  state = readHash();
  render();
});

function range() {
  if (state.view === "week") {
    const b = weekBounds(state.date);
    return [b[0], b[7]];
  }
  return dayBounds(state.date);
}

function showsNow() {
  if (state.view === "day") return state.date === today();
  if (state.view === "week") return weekDays(state.date).includes(today());
  return false;
}

// ---- sign-in -----------------------------------------------------------------------------------

function showSignIn(message = "") {
  signedIn = false;
  closeSheet();
  const msg = h("div", { class: "msg", text: message });
  const user = h("input", { name: "username", autocomplete: "username", placeholder: "Name", required: true });
  const pass = h("input", { name: "password", type: "password", autocomplete: "current-password", placeholder: "Password", required: true });
  const button = h("button", { class: "primary", type: "submit", text: "Sign in" });
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true;
      msg.textContent = "";
      try {
        const bytes = new TextEncoder().encode(`${user.value}:${pass.value}`);
        const res = await fetch("api/session", {
          method: "POST",
          credentials: "same-origin",
          headers: { ...CLIENT, Authorization: `Basic ${btoa(String.fromCharCode(...bytes))}` },
        });
        if (res.ok) return start();
        msg.textContent = { 401: "Wrong name or password", 403: "No access", 429: "Locked for 15 min" }[res.status] || `Error ${res.status}`;
      } catch {
        msg.textContent = "Server unreachable";
      } finally {
        button.disabled = false;
      }
    },
  }, h("img", { class: "tile", src: "icon.svg", alt: "xrnmoni" }), user, pass, button, msg);
  $app.replaceChildren(h("div", { class: "signin" }, form));
  user.focus();
}

async function signOut() {
  try {
    await fetch("api/session", { method: "DELETE", credentials: "same-origin", headers: CLIENT });
  } finally {
    showSignIn();
  }
}

// ---- the frame ---------------------------------------------------------------------------------

function applyTheme() {
  document.documentElement.dataset.theme = local.get("xrnmoni.theme") === "light" ? "light" : "dark";
}

function segmented(options, current, pick) {
  return h("div", { class: "seg-ctl", role: "group" },
    options.map(([value, label]) => h("button", {
      type: "button", "aria-pressed": String(value === current), text: label, onclick: () => pick(value),
    })));
}

function header() {
  const date = h("input", {
    type: "date", value: state.date, max: today(), "aria-label": "Date",
    onchange: (e) => parseKey(e.target.value) && go({ date: e.target.value }),
  });
  const step = state.view === "week" ? 7 : 1;
  const dates = state.view === "rules" ? null : h("div", { class: "dates" },
    h("button", { class: "icon-btn", type: "button", "aria-label": "Earlier", text: "‹", onclick: () => go({ date: shiftKey(state.date, -step) }) }),
    date,
    h("button", {
      class: "icon-btn", type: "button", "aria-label": "Later", text: "›",
      disabled: state.date >= today(),
      onclick: () => {
        const next = shiftKey(state.date, step);
        go({ date: next > today() ? today() : next });
      },
    }),
    h("button", { type: "button", text: "Today", disabled: state.date === today(), onclick: () => go({ date: today() }) }));
  const picker = h("select", { "aria-label": "Device", onchange: (e) => go({ device: e.target.value }) },
    h("option", { value: "", text: "All devices" }),
    devices.map((d) => h("option", { value: d.device, text: d.device, selected: d.device === state.device })));
  return h("header", { class: "bar" },
    h("div", { class: "left" },
      h("img", { class: "tile", src: "icon.svg", alt: "xrnmoni" }),
      segmented([["day", "Day"], ["week", "Week"], ["rules", "Rules"]], state.view, (view) => go({ view }))),
    state.view === "rules" ? null : h("div", { class: "filters" },
      dates, picker, segmented([["app", "Apps"], ["cat", "Categories"]], state.colour, (colour) => go({ colour }))),
    h("div", { class: "right" },
      state.view === "rules" ? null : h("button", { type: "button", text: "CSV", onclick: exportCsv }),
      h("button", {
        class: "icon-btn", type: "button", "aria-label": "Theme", title: "Theme",
        onclick: () => {
          local.set("xrnmoni.theme", document.documentElement.dataset.theme === "light" ? "dark" : "light");
          applyTheme();
        },
      }, icon(["M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z"], ["M8 2.5a5.5 5.5 0 0 0 0 11z", true])),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Sign out", title: "Sign out", onclick: signOut },
        icon(["M6.5 2.5H3.5v11h3"], ["M10 5l3 3-3 3"], ["M13 8H6.5"]))));
}

// [live]: new data for the same view, drawn over the old without a flash.
async function render(live = false) {
  if (!signedIn) return;
  const mine = ++renderCount;
  if (!live || !$main) {
    $main = h("main", { class: "loading" });
    $app.replaceChildren(header(), $main);
    if ($sheet) $app.append($sheet);
  }
  let body;
  try {
    body = state.view === "day" ? await dayView() : state.view === "week" ? await weekView() : await rulesView();
  } catch (e) {
    if (e instanceof Unauthorized) return;
    if (live) return; // keep what's on screen; the next change tries again
    body = h("div", { class: "error", text: e.message === "503" ? "Storage unreadable" : "Server unreachable" });
  }
  if (mine !== renderCount) return;
  $main.classList.remove("loading");
  $main.replaceChildren(...[body].flat());
}

function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => render(true), 1500);
}

// ---- the day -----------------------------------------------------------------------------------

function fig(label, value, colour) {
  return h("div", { class: "fig" },
    h("div", { class: "k" }, colour ? swatch(colour) : null, label),
    h("div", { class: "v", text: value }));
}

function swatch(colour) {
  const el = h("span", { class: "sw" });
  el.style.background = colour;
  return el;
}

function bar(fraction, colour) {
  const fill = h("div");
  fill.style.width = `${Math.max(1, Math.min(100, fraction * 100))}%`;
  fill.style.background = colour;
  return h("div", { class: "b" }, fill);
}

function online(info) {
  return info && nowS() - info.last_seen < ONLINE_S;
}

function nowText(info) {
  const n = info && info.now;
  if (!n || !online(info)) return "";
  if (n.type in AWAY) return AWAY[n.type];
  const what = n.type === "app" ? n.app : n.domain;
  const detail = n.domain && n.type === "app" ? n.domain : n.page || n.title;
  return [what, detail].filter(Boolean).join(" · ");
}

function ageSpan(info) {
  const el = h("span", { class: "age" });
  if (info) {
    el.dataset.seen = String(info.last_seen);
    el.textContent = ago(nowS() - info.last_seen);
  }
  return el;
}

function topNames(lists) {
  return merge(lists).filter((r) => r[0]).map((r) => r[0]);
}

async function dayView() {
  const [since, until] = dayBounds(state.date);
  const [day] = await Promise.all([getJSON(`api/day?since=${since}&until=${until}${deviceParam()}`), loadDevices()]);
  const shown = day.devices.filter((d) => d.active || d.segments.length);
  const isToday = state.date === today();
  const quiet = isToday ? day.devices.filter((d) => !shown.includes(d)).map((d) => quietCard(d)) : [];
  if (!shown.length) return [h("div", { class: "empty", text: "No data" }), ...quiet];
  const ctx = {
    since, until,
    isToday,
    apps: colours("app", topNames(shown.map((d) => d.apps))),
    cats: colours("cat", topNames(shown.map((d) => d.categories))),
  };
  const total = shown.reduce((a, d) => a + d.active, 0);
  const hero = h("section", { class: "hero" },
    h("div", { class: "v", text: dur(total) }),
    h("div", { class: "k", text: "Active" }),
    shown.length > 1 ? h("div", { class: "per" }, shown.map((d) => h("span", {}, `${d.device} `, h("b", { text: dur(d.active) })))) : null);
  return [hero, ...shown.map((d) => deviceCard(d, ctx)), ...quiet];
}

function deviceHead(d, isToday) {
  const info = devices.find((x) => x.device === d.device);
  return h("div", { class: "dev-head" },
    h("span", { class: `dot${online(info) ? " on" : ""}` }),
    h("span", { class: "name", text: d.device }),
    h("span", { class: "os", text: d.os || "" }),
    h("span", { class: "now", text: isToday ? nowText(info) : "" }),
    isToday ? ageSpan(info) : null);
}

// A device with nothing today: where it is, and when it was last heard from.
function quietCard(d) {
  return h("section", { class: "card quiet" }, deviceHead(d, true));
}

function deviceCard(d, ctx) {
  const head = deviceHead(d, ctx.isToday);
  const figs = h("div", { class: "figs" }, fig("Active", dur(d.active)));
  for (const [type, label] of Object.entries(AWAY)) {
    if (d.totals[type]) figs.append(fig(label, dur(d.totals[type]), `var(--${type})`));
  }
  for (const [type, secs] of Object.entries(d.totals)) {
    if (!KNOWN.has(type)) figs.append(fig(type, dur(secs)));
  }
  figs.append(fig("First", clock(d.first)), fig("Last", clock(d.last)));
  const scope = { ...ctx, device: d.device };
  return h("section", { class: "card" }, head, figs, timeline(d, scope),
    h("div", { class: "cols" },
      list("Apps", d.apps, "app", scope),
      list("Sites", d.sites, "site", scope)),
    categoryBlock(d.categories, scope));
}

function segmentColour(seg, ctx) {
  if (seg[2] in AWAY) return `var(--${seg[2]})`;
  if (state.colour === "cat") return (seg[4] && ctx.cats.get(seg[4])) || "var(--other)";
  return ctx.apps.get(seg[3]) || "var(--other)";
}

function timeline(d, ctx) {
  const span = ctx.until - ctx.since;
  const pct = (t) => ((t - ctx.since) / span) * 100;
  const place = (el, a, b) => {
    el.style.left = `${pct(a)}%`;
    el.style.width = `${Math.max(pct(b) - pct(a), 0.06)}%`;
    return el;
  };
  const track = h("div", { class: "track" });
  for (const seg of d.segments) {
    const el = place(h("div"), seg[0], seg[1]);
    el.style.background = segmentColour(seg, ctx);
    track.append(el);
  }
  for (const [t] of d.marks) {
    const el = h("div", { class: "mark" });
    el.style.left = `${pct(t)}%`;
    track.append(el);
  }
  const web = h("div", { class: "webtrack" });
  for (const w of d.web) {
    const el = place(h("div"), w[0], w[1]);
    el.style.background = state.colour === "cat" && w[3] ? ctx.cats.get(w[3]) || "var(--other)" : "var(--web)";
    web.append(el);
  }
  const ticks = h("div", { class: "ticks" });
  for (const [hour, t] of hourMarks(state.date, 3)) {
    const el = h("span", { class: hour % 6 ? "minor" : null, text: String(hour).padStart(2, "0") });
    el.style.left = `${pct(t)}%`;
    ticks.append(el);
  }
  const box = h("div", { class: "timeline" }, track, web, ticks);
  if (ctx.isToday) {
    const line = h("div", { class: "nowline" });
    line.style.left = `${Math.min(100, pct(nowS()))}%`;
    box.append(line);
  }
  // The pointer finds the stretch under it, however thin: a two-second window is still there.
  const find = (e) => {
    const onWeb = e.target === web || e.target.parentElement === web;
    const rect = track.getBoundingClientRect();
    const t = ctx.since + ((e.clientX - rect.left) / rect.width) * span;
    const slack = (4 / rect.width) * span;
    const list = onWeb ? d.web : d.segments;
    let best = null;
    let gap = slack;
    for (const s of list) {
      const off = t < s[0] ? s[0] - t : t > s[1] ? t - s[1] : 0;
      if (off <= gap) {
        best = s;
        gap = off;
        if (!off) break;
      }
    }
    if (!best && !onWeb) {
      const mark = d.marks.find(([m]) => Math.abs(m - t) <= slack);
      if (mark) return { mark };
    }
    return best && { seg: best, web: onWeb };
  };
  const tipFor = (hit) => {
    if (hit.mark) return [[clock(hit.mark[0]), hit.mark[1]]];
    const s = hit.seg;
    const label = hit.web ? s[2] : s[2] in AWAY ? AWAY[s[2]] : s[3];
    const cat = hit.web ? s[3] : s[4];
    return [[dur(s[1] - s[0]), `${clock(s[0])}–${clock(s[1])}`], [null, label], cat ? [null, cat] : null];
  };
  box.addEventListener("pointermove", (e) => {
    const hit = find(e);
    if (hit) showTip(e, tipFor(hit));
    else hideTip();
  });
  box.addEventListener("pointerleave", hideTip);
  box.addEventListener("click", (e) => {
    const hit = find(e);
    if (!hit || hit.mark) return;
    const s = hit.seg;
    if (hit.web) openDetail("site", s[2], ctx.device, [ctx.since, ctx.until]);
    else if (s[2] === "app") openDetail("app", s[3], ctx.device, [ctx.since, ctx.until]);
    else if (s[2] === "web") openDetail("site", s[3], ctx.device, [ctx.since, ctx.until]);
  });
  return box;
}

function list(title, rows, kind, ctx) {
  const box = h("div", { class: "list" }, h("h3", { text: title }));
  if (!rows.length) {
    box.append(h("div", { class: "none", text: "—" }));
    return box;
  }
  const max = rows[0][1] || 1;
  const colourOf = ([name, , cat]) => {
    if (state.colour === "cat") return (cat && ctx.cats.get(cat)) || "var(--other)";
    return kind === "app" ? ctx.apps.get(name) || "var(--other)" : "var(--web)";
  };
  const swatchOf = (r) => (kind === "site" && state.colour !== "cat" ? swatch("transparent") : swatch(colourOf(r)));
  const draw = (some) => some.map((r) => h("button", {
    class: "row", type: "button", title: r[0],
    onclick: () => openDetail(kind, r[0], ctx.device, [ctx.since, ctx.until]),
  }, swatchOf(r), h("span", { class: "n", text: r[0] }), bar(r[1] / max, colourOf(r)), h("span", { class: "d", text: dur(r[1]) })));
  box.append(...draw(rows.slice(0, SHOWN)));
  if (rows.length > SHOWN) {
    const more = h("button", {
      class: "more", type: "button", text: `+${rows.length - SHOWN}`,
      onclick: () => more.replaceWith(...draw(rows.slice(SHOWN))),
    });
    box.append(more);
  }
  return box;
}

function categoryBlock(cats, ctx) {
  if (!cats.some(([c]) => c)) return null;
  const total = sum(cats) || 1;
  const colourOf = (c) => (c && ctx.cats.get(c)) || "var(--other)";
  const split = h("div", { class: "split", role: "img", "aria-label": cats.map(([c, s]) => `${c || "Other"} ${dur(s)}`).join(", ") });
  for (const [c, s] of cats) {
    const part = h("div");
    part.style.flex = `${s} 0 0`;
    part.style.background = colourOf(c);
    split.append(part);
  }
  const rows = cats.map(([c, s]) => h("button", {
    class: "row cat", type: "button",
    onclick: () => openDetail("category", c, ctx.device, [ctx.since, ctx.until]),
  }, swatch(colourOf(c)), h("span", { class: "n", text: c || "Other" }),
  h("span", { class: "p", text: `${Math.round((100 * s) / total)}%` }), h("span", { class: "d", text: dur(s) })));
  return h("div", { class: "block" }, h("h3", { text: "Categories" }), split, rows);
}

// ---- details -----------------------------------------------------------------------------------

function closeSheet() {
  if ($sheet) $sheet.remove();
  $sheet = null;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSheet();
});

async function openDetail(kind, name, device, span) {
  const [since, until] = span || range();
  const doc = await getJSON(`api/detail?since=${since}&until=${until}&${kind}=${encodeURIComponent(name)}${deviceParam(device)}`);
  const max = doc.rows.length ? doc.rows[0][2] : 1;
  const rows = doc.rows.map(([text, extra, secs]) => {
    const parts = [h("span", { class: "n", text: text || "—" }), h("span", { class: "d", text: dur(secs) })];
    if (kind === "site" && extra) parts.push(h("span", { class: "u", text: extra, title: extra }));
    if (kind === "category") parts.push(h("span", { class: "u", text: extra === "site" ? "Site" : "App" }));
    parts.push(bar(secs / (max || 1), "var(--c1)"));
    return kind === "category"
      ? h("button", { class: "drow", type: "button", onclick: () => openDetail(extra, text, device, span) }, parts)
      : h("div", { class: "drow" }, parts);
  });
  closeSheet();
  const label = { app: "App", site: "Site", category: "Category" }[kind];
  $sheet = h("aside", { class: "sheet", "aria-label": name || "Other" },
    h("div", { class: "sheet-head" },
      h("div", { class: "t" },
        h("div", { class: "k", text: [label, device || "All devices", state.view === "week" && !span ? "Week" : dayLabel(state.date)].join(" · ") }),
        h("div", { class: "name", text: name || "Other" }),
        h("div", { class: "v", text: dur(doc.total) })),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close", text: "×", onclick: closeSheet })),
    rows.length ? rows : h("div", { class: "none", text: "—" }));
  $app.append($sheet);
}

// ---- the week ----------------------------------------------------------------------------------

function niceStep(maxS) {
  for (const step of [1800, 3600, 7200, 10800, 14400, 21600, 28800]) if (maxS / step <= 4) return step;
  return 43200;
}

async function weekView() {
  const bounds = weekBounds(state.date);
  const keys = weekDays(state.date);
  const [days, week] = await Promise.all([
    getJSON(`api/days?bounds=${bounds.join(",")}${deviceParam()}`),
    getJSON(`api/day?since=${bounds[0]}&until=${bounds[7]}&timeline=0${deviceParam()}`),
    loadDevices(),
  ]);
  const byCat = state.colour === "cat";
  // Each day's parts: per device, or per category over every device shown.
  const parts = days.days.map((day) => {
    if (!byCat) return Object.entries(day.devices).map(([name, d]) => [name, d.active]);
    return merge(Object.values(day.devices).map((d) => d.categories)).map(([c, s]) => [c, s]);
  });
  const totals = merge(parts);
  const names = totals.map((r) => r[0]);
  const palette = byCat ? colours("cat", names.filter(Boolean)) : colours("device", names);
  const colourOf = (n) => palette.get(n) || "var(--other)";
  const labelOf = (n) => (byCat ? n || "Other" : n);
  const order = new Map(names.map((n, i) => [n, i]));
  const dayTotal = parts.map((p) => p.reduce((a, [, s]) => a + s, 0));
  const max = Math.max(3600, ...dayTotal);
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;

  const plot = h("div", { class: "plot" });
  const yaxis = h("div", { class: "yaxis" });
  for (let v = step; v <= top; v += step) {
    const line = h("div", { class: "grid" });
    line.style.bottom = `${(100 * v) / top}%`;
    plot.append(line);
    const tick = h("span", { text: hours(v) });
    tick.style.bottom = `${(100 * v) / top}%`;
    yaxis.append(tick);
  }
  keys.forEach((key, i) => {
    const stack = h("div", { class: "stack" });
    stack.style.height = `${(100 * dayTotal[i]) / top}%`;
    const sorted = [...parts[i]].filter(([, s]) => s > 0).sort((a, b) => order.get(a[0]) - order.get(b[0]));
    for (const [n, s] of sorted) {
      const seg = h("div");
      seg.style.flex = `${s} 0 0`;
      seg.style.background = colourOf(n);
      seg.addEventListener("pointermove", (e) => showTip(e, [[dur(s), labelOf(n)], [null, dayLabel(key)]]));
      seg.addEventListener("pointerleave", hideTip);
      stack.append(seg);
    }
    const col = h("div", {
      class: "daycol", role: "button", tabindex: "0", "aria-label": `${dayLabel(key)} ${dur(dayTotal[i])}`,
      onclick: () => go({ view: "day", date: key }),
      onkeydown: (e) => (e.key === "Enter" || e.key === " ") && go({ view: "day", date: key }),
    }, dayTotal[i] ? h("div", { class: "cap", text: hours(dayTotal[i]) }) : null, stack);
    plot.append(col);
  });
  const xlabels = h("div", { class: "xlabels" }, h("span"),
    keys.map((key) => h("span", { class: key === today() ? "today" : null, text: dayLabel(key, { weekday: "short", day: "numeric" }) })));
  const keyRow = h("div", { class: "keys" }, totals.filter(([, s]) => s > 0).map(([n, s]) =>
    h("span", {}, swatch(colourOf(n)), labelOf(n), h("b", { text: dur(s) }))));

  const cols = byCat ? names : Object.keys(Object.assign({}, ...days.days.map((d) => d.devices))).sort();
  const cellOf = (i, n) => {
    const hit = parts[i].find(([m]) => m === n);
    return hit && hit[1] ? dur(hit[1]) : "—";
  };
  const table = h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", { text: "Day" }), cols.map((n) => h("th", { text: labelOf(n) })), h("th", { text: "Total" }))),
    h("tbody", {},
      keys.map((key, i) => h("tr", {}, h("td", { text: dayLabel(key) }), cols.map((n) => h("td", { text: cellOf(i, n) })),
        h("td", { text: dayTotal[i] ? dur(dayTotal[i]) : "—" }))),
      h("tr", { class: "total" }, h("td", { text: "Week" }),
        cols.map((n) => h("td", { text: dur((totals.find(([m]) => m === n) || [0, 0])[1]) })),
        h("td", { text: dur(dayTotal.reduce((a, b) => a + b, 0)) })))));

  const all = week.devices;
  const ctx = {
    since: bounds[0], until: bounds[7], device: state.device,
    apps: colours("app", topNames(all.map((d) => d.apps))),
    cats: colours("cat", topNames(all.map((d) => d.categories))),
  };
  const weekTotal = all.reduce((a, d) => a + d.active, 0);
  const hero = h("section", { class: "hero" },
    h("div", { class: "v", text: dur(weekTotal) }),
    h("div", { class: "k", text: `${dayLabel(keys[0], { day: "numeric", month: "short" })} – ${dayLabel(keys[6], { day: "numeric", month: "short" })}` }),
    h("div", { class: "per" }, h("span", {}, "Per day ", h("b", { text: dur(weekTotal / Math.max(1, keys.filter((k) => k <= today()).length)) }))));
  const cats = merge(all.map((d) => d.categories));
  return [
    hero,
    h("section", { class: "card" },
      h("div", { class: "week" }, yaxis, plot), xlabels, keyRow),
    h("section", { class: "card" }, table),
    h("section", { class: "card" },
      h("div", { class: "cols" },
        list("Apps", merge(all.map((d) => d.apps)), "app", ctx),
        list("Sites", merge(all.map((d) => d.sites)), "site", ctx)),
      categoryBlock(cats, ctx)),
  ];
}

// ---- category rules ----------------------------------------------------------------------------

async function rulesView() {
  const res = await api("api/categories");
  let etag = res.headers.get("ETag");
  const doc = await res.json();
  const rules = doc.rules.map((r) => ({ ...r }));
  const [since] = dayBounds(shiftKey(today(), -6));
  const [, until] = dayBounds(today());
  const recent = await getJSON(`api/day?since=${since}&until=${until}&timeline=0`);
  const known = () => [...new Set(rules.map((r) => r.category).filter(Boolean))].sort();

  const datalist = h("datalist", { id: "cats" });
  const box = h("div", { class: "rules" });
  const msg = h("span", { class: "msg", text: notice });
  notice = "";
  const save = h("button", { class: "primary", type: "button", text: "Save", disabled: true });
  const dirty = () => {
    save.disabled = false;
    msg.textContent = "";
  };
  const draw = () => {
    datalist.replaceChildren(...known().map((c) => h("option", { value: c })));
    box.replaceChildren(...rules.map((r, i) => h("div", { class: "rule" },
      h("select", { "aria-label": "Kind", onchange: (e) => { r.kind = e.target.value; dirty(); } },
        h("option", { value: "app", text: "App", selected: r.kind === "app" }),
        h("option", { value: "site", text: "Site", selected: r.kind === "site" })),
      h("input", { value: r.pattern, placeholder: r.kind === "site" ? "example.com" : "code", "aria-label": "Pattern", oninput: (e) => { r.pattern = e.target.value; dirty(); } }),
      h("input", { value: r.category, placeholder: "Category", list: "cats", "aria-label": "Category", oninput: (e) => { r.category = e.target.value; dirty(); } }),
      h("button", {
        class: "icon-btn up", type: "button", "aria-label": "Up", text: "↑", disabled: i === 0,
        onclick: () => { rules.splice(i - 1, 0, ...rules.splice(i, 1)); dirty(); draw(); },
      }),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Remove", text: "×", onclick: () => { rules.splice(i, 1); dirty(); draw(); } }))));
  };
  draw();
  save.addEventListener("click", async () => {
    const kept = rules.filter((r) => r.pattern.trim() && r.category.trim());
    save.disabled = true;
    try {
      const put = await fetch("api/categories", {
        method: "PUT", credentials: "same-origin",
        headers: { ...CLIENT, "Content-Type": "application/json", "If-Match": etag || "*" },
        body: JSON.stringify({ rules: kept }),
      });
      if (put.status === 401) return showSignIn();
      if (put.status === 412) {
        notice = "Changed elsewhere";
        return render();
      }
      if (!put.ok) {
        msg.textContent = (await put.text()) || `Error ${put.status}`;
        save.disabled = false;
        return;
      }
      etag = put.headers.get("ETag");
      render();
    } catch {
      msg.textContent = "Server unreachable";
      save.disabled = false;
    }
  });
  const add = h("button", { type: "button", text: "Add", onclick: () => { rules.push({ kind: "app", pattern: "", category: "" }); dirty(); draw(); } });

  const uncategorised = (kind, rows) => rows.filter((r) => !r[2] && r[0] && r[0] !== "?").slice(0, 15).map(([name, secs]) => {
    const input = h("input", { placeholder: "Category", list: "cats", "aria-label": `Category for ${name}` });
    return h("div", { class: "uncat" },
      h("span", { class: "n", text: name, title: name }), h("span", { class: "d", text: dur(secs) }), input,
      h("button", {
        type: "button", text: "Add",
        onclick: () => {
          if (!input.value.trim()) return input.focus();
          rules.push({ kind, pattern: name, category: input.value.trim() });
          dirty();
          draw();
          input.closest(".uncat").remove();
        },
      }));
  });
  const apps = uncategorised("app", merge(recent.devices.map((d) => d.apps)));
  const sites = uncategorised("site", merge(recent.devices.map((d) => d.sites)));
  return [
    h("section", { class: "card" }, h("div", { class: "list" }, h("h3", { text: "Rules" })), box, datalist,
      h("div", { class: "actions" }, add, save, msg)),
    apps.length || sites.length ? h("section", { class: "card" },
      h("div", { class: "cols" },
        h("div", { class: "list" }, h("h3", { text: "Apps without one · 7 days" }), apps.length ? apps : h("div", { class: "none", text: "—" })),
        h("div", { class: "list" }, h("h3", { text: "Sites without one · 7 days" }), sites.length ? sites : h("div", { class: "none", text: "—" })))) : null,
  ];
}

// ---- export, tooltips, live updates ------------------------------------------------------------

async function exportCsv() {
  const [since, until] = range();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  const res = await api(`api/export?since=${since}&until=${until}&tz=${encodeURIComponent(tz)}${deviceParam()}`);
  const url = URL.createObjectURL(await res.blob());
  const name = `xrnmoni-${state.view === "week" ? `week-${weekDays(state.date)[0]}` : state.date}${state.device ? `-${state.device}` : ""}.csv`;
  const a = h("a", { href: url, download: name.replace(/[^\w.-]+/g, "_") });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// [lines]: [value, label] pairs; the value is the strong part.
function showTip(e, lines) {
  $tip.replaceChildren(...lines.filter(Boolean).map(([value, label]) => h("div", {},
    value ? h("span", { class: "v", text: value }) : null, value && label ? " " : null,
    label ? h("span", { class: value ? "" : "l", text: label }) : null)));
  $tip.hidden = false;
  const pad = 14;
  const w = $tip.offsetWidth;
  const ht = $tip.offsetHeight;
  let x = e.clientX + pad;
  let y = e.clientY + pad;
  if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
  if (y + ht > window.innerHeight - 8) y = e.clientY - ht - pad;
  $tip.style.left = `${Math.max(8, x)}px`;
  $tip.style.top = `${Math.max(8, y)}px`;
}

function hideTip() {
  $tip.hidden = true;
}

// The server answers when an agent posts (or after 25 s), and the view is drawn again if it shows now.
async function watch() {
  if (watching) return;
  watching = true;
  try {
    while (signedIn) {
      try {
        const doc = await getJSON(`api/changes?seq=${seq}&wait=25`);
        if (doc.seq !== seq) {
          seq = doc.seq;
          if (showsNow()) refreshSoon();
        }
      } catch (e) {
        if (e instanceof Unauthorized) return;
        await sleep(5000);
      }
    }
  } finally {
    watching = false;
  }
}

setInterval(() => {
  for (const el of document.querySelectorAll(".age[data-seen]")) el.textContent = ago(nowS() - Number(el.dataset.seen));
}, 1000);

// The now line and the online dots move on even when no agent posts.
setInterval(() => {
  if (signedIn && showsNow() && !document.hidden) render(true);
}, 60000);

async function start() {
  applyTheme();
  try {
    await loadDevices();
  } catch (e) {
    if (!(e instanceof Unauthorized)) $app.replaceChildren(h("div", { class: "error", text: "Server unreachable" }));
    return;
  }
  signedIn = true;
  state = readHash();
  render();
  watch();
}

start();
