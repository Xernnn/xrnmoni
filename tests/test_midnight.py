"""Intervals are split at local midnight, so each day's total is that day's time."""
import datetime
import http.client
import json
import threading
import time
import unittest

from agent_helpers import config

import agent
from test_tracker import Clock, FakeBackend

MIDNIGHT = time.mktime((2026, 3, 11, 0, 0, 0, 0, 0, -1))   # a past local midnight


def day(ts):
    return datetime.datetime.fromtimestamp(ts).date()


class MidnightTest(unittest.TestCase):
    def assert_within_days(self, events):
        for e in events:
            # an interval may end exactly at midnight, but never runs into the next day
            self.assertEqual(day(e["ts_start"]), day(e["ts_end"] - 1e-6), e)

    def test_pieces_are_cut_at_midnight(self):
        self.assertEqual(agent.pieces(MIDNIGHT - 30, MIDNIGHT + 45),
                         [(MIDNIGHT - 30, MIDNIGHT), (MIDNIGHT, MIDNIGHT + 45)])
        self.assertEqual(agent.pieces(MIDNIGHT - 30, MIDNIGHT),
                         [(MIDNIGHT - 30, MIDNIGHT)])

    def test_an_app_interval_over_midnight_is_split(self):
        events, clock, backend = [], Clock(MIDNIGHT - 50), FakeBackend()
        tracker = agent.Tracker(config(), events.extend, backend, now=clock)
        while clock.t < MIDNIGHT + 100:
            tracker.tick()
            clock.t += 2
        tracker.close()
        self.assert_within_days(events)
        self.assertIn(MIDNIGHT, [e["ts_end"] for e in events])
        before = sum(e["duration"] for e in events if e["ts_end"] <= MIDNIGHT)
        self.assertEqual(before, 50)
        self.assertEqual(sum(e["duration"] for e in events), 150)
        self.assertEqual(len({e["id"] for e in events}), len(events))

    def test_a_night_asleep_is_split_too(self):
        events, clock, elapsed = [], Clock(MIDNIGHT - 3630), Clock(100.0)
        tracker = agent.Tracker(config(), events.extend, FakeBackend(), now=clock,
                                clock=elapsed)
        tracker.tick()
        clock.t += 8 * 3600
        elapsed.t += 8 * 3600
        tracker.tick()
        self.assert_within_days([e for e in events if e["type"] == "sleep"])
        before = sum(e["duration"] for e in events if e["type"] == "sleep"
                     and e["ts_end"] <= MIDNIGHT)
        self.assertEqual(before, 3630)

    def test_a_page_interval_over_midnight_is_split(self):
        class Collect(list):
            put = list.append
        sent = Collect()
        origin, secret = "chrome-extension://abcdefghijklmnopabcdefghijklmnop", "s" * 20
        bridge = agent.BrowserBridge(config(bridge_origins=[origin], bridge_secret=secret), sent)
        threading.Thread(target=bridge.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", bridge.server_address[1], timeout=5)
        c.request("POST", "/browser", json.dumps(
            {"url": "https://example.com/", "title": "t", "ts_start": MIDNIGHT - 10,
             "ts_end": MIDNIGHT + 10}), {"Origin": origin, "X-Xrnmoni-Secret": secret})
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual([(e["ts_start"], e["ts_end"]) for e in sent],
                         [(MIDNIGHT - 10, MIDNIGHT), (MIDNIGHT, MIDNIGHT + 10)])


if __name__ == "__main__":
    unittest.main()
