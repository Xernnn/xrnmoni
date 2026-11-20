"""Every event carries an id, so the server can drop one it already has."""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest

from agent_helpers import config

import agent

HEX32 = re.compile(r"[0-9a-f]{32}")


class EventIdTest(unittest.TestCase):
    def setUp(self):
        self.spool = agent.Spool(os.path.join(tempfile.mkdtemp(), "spool.sqlite"))
        self.addCleanup(self.spool.close)
        self.sender = agent.Sender(config(), self.spool)

    def ids(self):
        return [e["id"] for _, e in self.spool.peek(100)]

    def test_events_get_an_id_when_they_are_made(self):
        self.sender.put({"type": "app", "app": "editor", "ts_start": 1.0e9, "ts_end": 1.0e9 + 5})
        self.sender.put({"type": "app", "app": "editor", "ts_start": 1.0e9, "ts_end": 1.0e9 + 5})
        ids = self.ids()
        self.assertTrue(all(HEX32.fullmatch(i) for i in ids), ids)
        self.assertEqual(len(set(ids)), 2)

    def test_an_id_survives_the_spool(self):
        self.sender.put({"type": "app", "id": "ab" * 16, "ts_start": 1.0e9})
        self.assertEqual(self.ids(), ["ab" * 16])

    def test_the_boot_event_has_the_same_id_on_every_start(self):
        a = agent.boot_event("pc", 1.7e9)
        b = agent.boot_event("pc", 1.7e9)
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(HEX32.fullmatch(a["id"]))
        self.assertNotEqual(a["id"], agent.boot_event("pc", 1.7e9 + 60)["id"])
        self.assertNotEqual(a["id"], agent.boot_event("other", 1.7e9)["id"])

    def test_a_boot_time_that_moves_by_a_second_is_still_the_same_boot(self):
        # The boot time is worked out as the clock minus the uptime, so it moves by a second
        # or so from one start to the next, and each such start made a boot row of its own.
        a = agent.boot_event("pc", 1.7e9 + 0.6)
        self.assertEqual(a["id"], agent.boot_event("pc", 1.7e9 + 1.4)["id"])
        self.assertEqual(1.7e9 + 0.6, a["ts_start"])

    def test_a_boot_the_kernel_names_is_known_by_that_name(self):
        name = "0b6f2c4e-1d7a-4f39-9c8e-5a2b7d4e6f10"
        a = agent.boot_event("pc", 1.7e9, name)
        self.assertEqual(a["id"], agent.boot_event("pc", 1.7e9 + 9, name)["id"])   # a clock step
        self.assertNotEqual(a["id"], agent.boot_event("pc", 1.7e9, name.replace("0b", "0c"))["id"])
        self.assertNotEqual(a["id"], agent.boot_event("other", 1.7e9, name)["id"])

    def test_linux_reads_the_boot_id(self):
        import platform_linux
        path = os.path.join(tempfile.mkdtemp(), "boot_id")
        with open(path, "w") as f:
            f.write("0b6f2c4e-1d7a-4f39-9c8e-5a2b7d4e6f10\n")
        self.assertEqual("0b6f2c4e-1d7a-4f39-9c8e-5a2b7d4e6f10", platform_linux.boot_id(path))
        self.assertIsNone(platform_linux.boot_id(path + ".missing"))


class BridgeIdTest(unittest.TestCase):
    ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def setUp(self):
        self.spool = agent.Spool(os.path.join(tempfile.mkdtemp(), "spool.sqlite"))
        self.addCleanup(self.spool.close)
        cfg = config(bridge_origins=[self.ORIGIN], bridge_secret="s" * 24)
        self.bridge = agent.BrowserBridge(cfg, agent.Sender(cfg, self.spool))
        threading.Thread(target=self.bridge.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)

    def post(self, item):
        c = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=5)
        c.request("POST", "/browser", json.dumps(item),
                  {"Content-Type": "application/json", "Origin": self.ORIGIN,
                   "X-Xrnmoni-Secret": "s" * 24})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_browser_events_carry_an_id(self):
        t = 1.7e9
        page = {"url": "https://example.com/", "title": "t", "ts_start": t, "ts_end": t + 20}
        self.assertEqual(self.post(page), 200)
        self.assertEqual(self.post(dict(page, id="cd" * 16)), 200)
        self.assertEqual(self.post(dict(page, id="not an id")), 200)
        ids = [e["id"] for _, e in self.spool.peek(10)]
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(HEX32.fullmatch(i) for i in ids), ids)
        self.assertEqual(ids[1], "cd" * 16)
        self.assertEqual(len(set(ids)), 3)


if __name__ == "__main__":
    unittest.main()
