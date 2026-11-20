// The dashboard's local dates, in time zones on both sides of UTC.
//
//     node --test server/test_web.mjs
//
// Each case runs in its own node with TZ set, because a process reads its zone once.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const dates = pathToFileURL(path.join(here, "web", "dates.js")).href;

function inZone(tz, code) {
  const script = `import * as d from ${JSON.stringify(dates)};
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    console.log(JSON.stringify({ zone, out: (() => { ${code} })() }));`;
  const out = execFileSync(process.execPath, ["--input-type=module", "-e", script], {
    env: { ...process.env, TZ: tz },
    encoding: "utf8",
  });
  return JSON.parse(out);
}

function check(t, tz, code) {
  const got = inZone(tz, code);
  if (got.zone !== tz) {
    t.skip(`this node can't be put in ${tz}`);
    return null;
  }
  return got.out;
}

test("just after midnight in Sydney, today is the local date, not the UTC one", (t) => {
  // 00:27 on 20 November in Sydney is 13:27 on the 19th in UTC.
  const out = check(t, "Australia/Sydney", `
    const now = new Date(Date.UTC(2026, 10, 19, 13, 27));
    return { today: d.today(now), utc: now.toISOString().slice(0, 10), bounds: d.dayBounds(d.today(now)) };`);
  if (!out) return;
  assert.equal(out.today, "2026-11-20");
  assert.equal(out.utc, "2026-11-19"); // what the first dashboard showed
  assert.deepEqual(out.bounds, [Date.UTC(2026, 10, 19, 13) / 1000, Date.UTC(2026, 10, 20, 13) / 1000]);
});

test("late in the evening west of UTC, today is still the local date", (t) => {
  const out = check(t, "America/Los_Angeles", `
    const now = new Date(Date.UTC(2026, 10, 20, 7, 30));
    return d.today(now);`);
  if (out) assert.equal(out, "2026-11-19");
});

test("a day with a daylight-saving change is that many hours long", (t) => {
  const out = check(t, "Australia/Sydney", `
    return [d.dayBounds("2026-10-04"), d.dayBounds("2026-04-05"), d.shiftKey("2026-10-03", 1)];`);
  if (!out) return;
  const [spring, autumn, next] = out;
  assert.equal(spring[1] - spring[0], 23 * 3600);
  assert.equal(autumn[1] - autumn[0], 25 * 3600);
  assert.equal(next, "2026-10-04");
});

test("a week runs Monday to Sunday around the day picked", (t) => {
  const out = check(t, "Europe/Berlin", `
    return { days: d.weekDays("2026-11-20"), bounds: d.weekBounds("2026-11-20") };`);
  if (!out) return;
  assert.deepEqual(out.days, ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]);
  assert.equal(out.bounds.length, 8);
  assert.equal(out.bounds[0], Date.UTC(2026, 10, 15, 23) / 1000);
});

test("only real dates are taken", (t) => {
  const out = check(t, "UTC", `return ["2026-02-30", "2026-1-5", "x", "2026-02-28"].map((k) => d.parseKey(k) !== null);`);
  if (out) assert.deepEqual(out, [false, false, false, true]);
});
