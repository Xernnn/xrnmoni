// Options: the agent's bridge port and the shared secret (bridge_secret in the
// agent's config). The origin shown here goes into the config's bridge_origins.

const $ = (id) => document.getElementById(id);
const DEFAULTS = { bridgePort: 8129, bridgeSecret: "", lastPost: null };

function showLast(p) {
  $("last").textContent = p ? `${new Date(p.at).toLocaleTimeString()} · ${p.status}` : "—";
}

async function load() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $("port").value = s.bridgePort;
  $("secret").value = s.bridgeSecret;
  $("origin").textContent = location.origin;
  showLast(s.lastPost);
}

$("save").addEventListener("click", async () => {
  const port = parseInt($("port").value, 10);
  await chrome.storage.local.set({
    bridgePort: port >= 1 && port <= 65535 ? port : DEFAULTS.bridgePort,
    bridgeSecret: $("secret").value.trim(),
  });
  $("saved").textContent = "Saved";
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.lastPost) showLast(changes.lastPost.newValue);
});

load();
