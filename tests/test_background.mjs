// The extension's background script in a vm with a stand-in chrome.*: what it posts to the agent.
//
//     node --test tests/test_background.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");

function listener() {
  const fns = [];
  return { addListener: (fn) => fns.push(fn), fire: (...a) => Promise.all(fns.map((fn) => fn(...a))) };
}

// Loads background.js with [tab] in front; returns the posts it makes and a way to move the clock.
async function load(tab) {
  const posts = [];
  let clock = Date.parse("2027-01-15T12:00:00Z");
  const session = {};
  const local = { bridgePort: 8129, bridgeSecret: "test-secret" };
  const events = {
    onActivated: listener(), onUpdated: listener(), onFocusChanged: listener(),
    onStateChanged: listener(), onStartup: listener(), onInstalled: listener(), onAlarm: listener(),
    onClicked: listener(),
  };
  const opened = [];
  const chrome = {
    tabs: {
      onActivated: events.onActivated, onUpdated: events.onUpdated,
      query: async () => (state.tab ? [state.tab] : []),
    },
    windows: { onFocusChanged: events.onFocusChanged, getLastFocused: async () => ({ focused: true }) },
    idle: { setDetectionInterval() {}, onStateChanged: events.onStateChanged },
    runtime: { onStartup: events.onStartup, onInstalled: events.onInstalled, openOptionsPage: () => opened.push("options") },
    action: { onClicked: events.onClicked },
    alarms: { create() {}, onAlarm: events.onAlarm },
    storage: {
      session: { get: async () => ({ ...session }), set: async (o) => Object.assign(session, o) },
      local: { get: async (d) => ({ ...d, ...local }), set: async (o) => Object.assign(local, o) },
    },
  };
  const state = { tab };
  const context = {
    chrome, crypto, URL, console,
    Date: { now: () => clock },
    fetch: async (url, init) => {
      posts.push({ url, headers: init.headers, body: JSON.parse(init.body) });
      return { status: 200 };
    },
  };
  vm.runInNewContext(SOURCE, context);
  await settle();
  return {
    posts, events, state, opened,
    advance(seconds) { clock += seconds * 1000; },
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

test("a private window's tab is posted as its time and Private, never its address or title", async () => {
  const ext = await load({ url: "https://mail.example.com/inbox/42", title: "Inbox (3) - alex", incognito: true });
  ext.advance(40);
  await ext.events.onAlarm.fire({ name: "heartbeat" });
  await settle(); // the alarm's handler doesn't wait for its post
  ext.advance(20);
  await ext.events.onStateChanged.fire("idle");
  assert.equal(ext.posts.length, 2);
  for (const { body } of ext.posts) {
    assert.equal(body.url, null);
    assert.equal(body.domain, null);
    assert.equal(body.title, "Private");
    assert.equal(body.private, true);
    assert.doesNotMatch(JSON.stringify(body), /example|Inbox|alex/);
  }
  assert.deepEqual(ext.posts.map((p) => p.body.duration), [40, 20]);
});

test("an ordinary tab is posted with its address and title", async () => {
  const ext = await load({ url: "https://news.example.com/today", title: "Today", incognito: false });
  ext.advance(15);
  await ext.events.onStateChanged.fire("locked");
  assert.equal(ext.posts.length, 1);
  const { body, headers } = ext.posts[0];
  assert.equal(body.url, "https://news.example.com/today");
  assert.equal(body.title, "Today");
  assert.equal(body.domain, "news.example.com");
  assert.equal(body.private, undefined);
  assert.equal(headers["X-Xrnmoni-Secret"], "test-secret");
});

test("moving from an ordinary tab to a private one ends the ordinary page there", async () => {
  const ext = await load({ url: "https://news.example.com/today", title: "Today", incognito: false });
  ext.advance(10);
  ext.state.tab = { url: "https://shop.example.com/", title: "Shop", incognito: true };
  await ext.events.onActivated.fire({});
  ext.advance(5);
  await ext.events.onStateChanged.fire("idle");
  assert.deepEqual(ext.posts.map((p) => [p.body.title, p.body.url, p.body.duration]),
    [["Today", "https://news.example.com/today", 10], ["Private", null, 5]]);
});

test("the toolbar button opens the options", async () => {
  const ext = await load(null);
  await ext.events.onClicked.fire({});
  assert.deepEqual(ext.opened, ["options"]);
  assert.equal(ext.posts.length, 0);
});
