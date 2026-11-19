"""The extension in a real (headless) Chromium, posting to the agent's real bridge.

Needs Playwright (`pip install playwright`) and a Chromium it can start: its own
download, or one found under ~/.cache/ms-playwright. Skipped when neither is there,
as in CI. Firefox/Zen aren't run (the manifest test covers what differs for them).

    python -m unittest tests/test_extension_chromium.py
"""
import glob
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_helpers import ROOT, config

import agent

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

EXT = os.path.join(ROOT, "extension")
SECRET = "chromium-test-secret"


def chromium_path():
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else None


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.split("?")[0].strip("/") or "index"
        body = f"<!doctype html><title>Page {name}</title><p>{name}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Collect:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.1)
    return cond()


@unittest.skipUnless(sync_playwright, "Playwright is not installed")
class ChromiumExtensionTest(unittest.TestCase):
    def setUp(self):
        self.sender = Collect()
        self.bridge = agent.BrowserBridge(config(bridge_secret=SECRET), self.sender)
        threading.Thread(target=self.bridge.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)
        self.pages = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=self.pages.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.pages.server_close)
        self.addCleanup(self.pages.shutdown)
        self.profile = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.profile, True)

    def launch(self, pw):
        args = dict(headless=True, args=[f"--disable-extensions-except={EXT}",
                                         f"--load-extension={EXT}"])
        try:
            return pw.chromium.launch_persistent_context(self.profile, **args)
        except Exception:
            path = chromium_path()
            if not path:
                self.skipTest("no Chromium for Playwright")
            return pw.chromium.launch_persistent_context(self.profile, executable_path=path,
                                                         **args)

    def test_it_posts_page_intervals_with_the_secret(self):
        base = f"http://127.0.0.1:{self.pages.server_address[1]}"
        with sync_playwright() as pw:
            ctx = self.launch(pw)
            try:
                worker = ctx.service_workers[0] if ctx.service_workers else \
                    ctx.wait_for_event("serviceworker", timeout=10000)
                ext_id = re.match(r"chrome-extension://([a-p]{32})/", worker.url).group(1)
                origin = f"chrome-extension://{ext_id}"

                options = ctx.new_page()
                options.goto(f"{origin}/options.html")
                self.assertEqual(options.text_content("#origin"), origin)
                options.fill("#port", str(self.bridge.server_address[1]))
                options.fill("#secret", SECRET)
                options.click("#save")
                options.wait_for_selector("#saved:has-text('Saved')")

                page = ctx.new_page()
                page.goto(f"{base}/first?q=private#part")
                time.sleep(1.5)
                page.goto(f"{base}/second")          # closes the first page's interval
                # not in bridge_origins yet: refused, and the options page says so
                self.assertTrue(wait_for(lambda: "403" in options.text_content("#last")))
                self.assertEqual(self.sender.events, [])

                self.bridge.origins.add(origin)      # the user adds it to the config
                time.sleep(1.5)
                page.goto(f"{base}/third")           # closes the second page's interval
                got = wait_for(lambda: [e for e in self.sender.events
                                        if e["url"] == f"{base}/second"])
                self.assertTrue(got, self.sender.events)
                e = got[0]
                self.assertRegex(e["id"], r"^[0-9a-f]{32}$")
                self.assertEqual(e["title"], "Page second")
                self.assertEqual(e["domain"], "127.0.0.1")
                self.assertGreater(e["ts_end"] - e["ts_start"], 1.0)
                self.assertTrue(wait_for(lambda: "200" in options.text_content("#last")))
            finally:
                ctx.close()


if __name__ == "__main__":
    unittest.main()
