// The Get page: what a device needs to be set up (the agents' installers, the extension, the phone
// app), each with its size and SHA-256. It opens for a signed-in viewer, or with the device token
// that device will post with, so the owner's password never has to be typed on a child's device.
// The token stays in this page's memory only.

const CLIENT = { "X-Xrn-Client": "xrnmoni" };
const $app = document.getElementById("app");

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

try {
  document.documentElement.dataset.theme = JSON.parse(localStorage.getItem("xrnmoni.theme")) === "light" ? "light" : "dark";
} catch {
  // no storage: the dark theme
}

function size(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// The address the agents and the phone app post to: this page's folder.
function hubAddress() {
  return new URL(".", document.baseURI).href.replace(/\/$/, "");
}

function copyButton(text) {
  const button = h("button", {
    type: "button", text: "Copy",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        button.textContent = "—";
      }
    },
  });
  return button;
}

function show(files) {
  const hub = hubAddress();
  const rows = files.map((f) => h("div", { class: "file" },
    h("div", { class: "head" }, h("span", { class: "kind", text: f.kind }), h("span", { class: "size", text: size(f.size) })),
    h("div", { class: "name", text: f.name }),
    h("code", { class: "sum", text: f.sha256, title: "SHA-256" }),
    h("a", { class: "button", href: f.href, download: f.name, text: "Download" })));
  $app.replaceChildren(h("main", { class: "get" },
    h("header", { class: "gethead" }, h("img", { class: "tile", src: "icon.svg", alt: "xrnmoni" }), h("h1", { text: "Get" })),
    h("section", { class: "card hub" }, h("div", { class: "k", text: "Hub" }), h("code", { class: "secret", text: hub }), copyButton(hub)),
    h("section", { class: "card files" }, rows.length ? rows : h("div", { class: "none", text: "—" }))));
}

function askToken(message = "") {
  const msg = h("div", { class: "msg", text: message });
  const token = h("input", { name: "token", type: "password", autocomplete: "off", spellcheck: "false", placeholder: "Token", required: true });
  const button = h("button", { class: "primary", type: "submit", text: "Show" });
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true;
      await load(token.value.trim(), msg);
      button.disabled = false;
    },
  }, h("img", { class: "tile", src: "icon.svg", alt: "xrnmoni" }), token, button, msg,
  h("a", { class: "alt", href: "./", text: "Sign in" }));
  $app.replaceChildren(h("div", { class: "signin" }, form));
  token.focus();
}

async function load(token, msg) {
  let res;
  try {
    res = await fetch("api/downloads", {
      credentials: "same-origin",
      headers: token ? { ...CLIENT, Authorization: `Bearer ${token}` } : CLIENT,
    });
  } catch {
    if (msg) msg.textContent = "Server unreachable";
    else $app.replaceChildren(h("div", { class: "error", text: "Server unreachable" }));
    return;
  }
  if (res.ok) return show((await res.json()).files);
  const said = { 401: token ? "Token refused" : "", 429: "Locked for 15 min" }[res.status] ?? `Error ${res.status}`;
  if (msg) msg.textContent = said;
  else askToken(said);
}

load("", null);
