// The icons: every size a browser may draw, in both the manifest's "icons" and the toolbar
// button's "action.default_icon", each file that size, and the tile reaching the edges of its
// canvas. Chromium picks the next size up for its slot and scales it down; on Android the toolbar
// slot is 20 dp, 55 px on a 2.75x phone, so a set stopping at 32 was stretched and blurred there.
// scripts/extension-icons.sh renders the files from the tile. Run: node --test tests/test_icons.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";

const read = (name) => readFileSync(new URL(`../extension/${name}`, import.meta.url));
const manifest = JSON.parse(read("manifest.json"));
const SIZES = ["16", "24", "32", "48", "64", "96", "128", "256"];

/** A PNG's width, height and the alpha at (x, y): 8-bit RGBA, not interlaced (rsvg-convert's). */
function png(buf) {
  assert.equal(buf.toString("latin1", 1, 4), "PNG");
  const width = buf.readUInt32BE(16);
  const height = buf.readUInt32BE(20);
  assert.deepEqual([buf[24], buf[25], buf[28]], [8, 6, 0], "8-bit RGBA, not interlaced");
  const idat = [];
  for (let at = 8; at < buf.length; ) {
    const len = buf.readUInt32BE(at);
    if (buf.toString("latin1", at + 4, at + 8) === "IDAT") idat.push(buf.subarray(at + 8, at + 8 + len));
    at += 12 + len;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * 4;
  const px = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    for (let i = 0; i < stride; i++) {
      const v = raw[y * (stride + 1) + 1 + i];
      const a = i >= 4 ? px[y * stride + i - 4] : 0;
      const b = y ? px[(y - 1) * stride + i] : 0;
      const c = i >= 4 && y ? px[(y - 1) * stride + i - 4] : 0;
      const p = a + b - c;
      const paeth = Math.abs(p - a) <= Math.abs(p - b) && Math.abs(p - a) <= Math.abs(p - c) ? a : Math.abs(p - b) <= Math.abs(p - c) ? b : c;
      px[y * stride + i] = (v + [0, a, b, (a + b) >> 1, paeth][filter]) & 255;
    }
  }
  return { width, height, alpha: (x, y) => px[y * stride + x * 4 + 3] };
}

test("the manifest names every size, for the extension and for its toolbar button", () => {
  assert.deepEqual(Object.keys(manifest.icons), SIZES);
  assert.deepEqual(Object.keys(manifest.action.default_icon), SIZES);
  assert.deepEqual(manifest.action.default_icon, manifest.icons);
});

test("each icon is the size it is named for, and the tile fills it to the edges", () => {
  for (const [size, file] of Object.entries(manifest.icons)) {
    const { width, height, alpha } = png(read(file));
    assert.deepEqual([width, height], [+size, +size], file);
    const mid = width >> 1;
    for (const [x, y] of [[mid, 0], [mid, height - 1], [0, mid], [width - 1, mid]]) assert.ok(alpha(x, y) > 200, `${file} reaches (${x}, ${y})`);
    assert.ok(alpha(0, 0) < 128, `${file} has the tile's rounded corner`);
  }
});

test("the big icons are the xrnmoni tile itself", () => {
  const tile = readFileSync(new URL("../extension/icons/icon.svg", import.meta.url), "utf8");
  assert.match(tile, /fill="#039B7D"/);
  assert.match(tile, /<clipPath id="xrnmoni-tile">/);
});
