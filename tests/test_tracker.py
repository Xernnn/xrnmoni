"""The tracker turns what the platform reports into closed intervals of at most 60 s."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from agent_helpers import AGENT, config

import agent

T0 = time.mktime((2027, 1, 15, 12, 0, 0, 0, 0, -1))  # local noon, far from midnight


class FakeBackend:
    NAME = "fake"

    def __init__(self):
        self.window = ("editor", "notes")
        self.idle = 0.0

    def get_active_window(self):
        return self.window

    def get_idle_seconds(self):
        return self.idle


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.backend = FakeBackend()
        self.clock = Clock(T0)
        self.tracker = agent.Tracker(config(poll_interval=2.0), self.events.extend,
                                     self.backend, now=self.clock)

    def run_for(self, seconds, step=2.0):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.tracker.tick()
            self.clock.t += step

    def spans(self, kind=None):
        return [(e["type"], e["ts_start"] - T0, e["ts_end"] - T0) for e in self.events
                if kind is None or e["type"] == kind]

    def test_a_long_stay_in_one_window_is_sent_every_minute(self):
        self.run_for(130)
        self.assertEqual(self.spans(), [("app", 0, 60), ("app", 60, 120)])
        self.tracker.close()
        self.assertEqual(self.spans()[-1], ("app", 120, 130))
        self.assertTrue(all(e["app"] == "editor" and e["title"] == "notes" for e in self.events))
        self.assertTrue(all(e["duration"] == e["ts_end"] - e["ts_start"] for e in self.events))
        self.assertEqual(len({e["id"] for e in self.events}), 3)

    def test_a_window_switch_closes_the_interval_at_the_switch(self):
        self.run_for(10)
        self.backend.window = ("browser", "news")
        self.run_for(10)
        self.tracker.close()
        self.assertEqual(self.spans(), [("app", 0, 10), ("app", 10, 20)])
        self.assertEqual(self.events[1]["app"], "browser")

    def test_long_idle_is_sent_every_minute_too(self):
        self.run_for(10)
        self.backend.idle = 60.0
        self.run_for(150)
        kinds = [k for k, _, _ in self.spans()]
        self.assertGreaterEqual(kinds.count("afk"), 2)
        self.assertTrue(all(b - a <= 60 for _, a, b in self.spans()))


FAKE_MAIN = textwrap.dedent("""
    import sys, types
    sys.path.insert(0, sys.argv[1])
    import agent
    fake = types.SimpleNamespace(NAME="fake", get_idle_seconds=lambda: 0.0,
                                 get_active_window=lambda: ("editor", "notes"))
    agent.backend = fake
    sys.argv = ["agent.py", "--config", sys.argv[2]]
    agent.main()
""")


@unittest.skipIf(sys.platform.startswith("win"), "POSIX signals")
class SignalFlushTest(unittest.TestCase):
    def run_agent_and_signal(self, sig):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "config.toml")
        spool = os.path.join(d, "spool.sqlite")
        with open(cfg, "w") as f:
            f.write(f'server_url = "http://127.0.0.1:9"\ntoken = "{"t" * 20}"\n'
                    f'device = "test-pc"\npoll_interval = 0.2\nflush_interval = 0.2\n'
                    f'bridge_port = 0\nspool_path = "{spool}"\n')
        p = subprocess.Popen([sys.executable, "-c", FAKE_MAIN, AGENT, cfg],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertIn("[xrnmoni]", p.stdout.readline())
        time.sleep(1.5)
        killed_at = time.time()
        p.send_signal(sig)
        p.wait(timeout=15)
        s = agent.Spool(spool)
        self.addCleanup(s.close)
        return killed_at, [e for _, e in s.peek(100)]

    def test_a_signal_writes_the_open_interval_to_the_spool(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig.name):
                killed_at, events = self.run_agent_and_signal(sig)
                apps = [e for e in events if e["type"] == "app"]
                self.assertEqual(len(apps), 1, events)
                self.assertGreater(apps[0]["duration"], 1.0)
                self.assertAlmostEqual(apps[0]["ts_end"], killed_at, delta=1.0)
                self.assertIn("shutdown", [e["type"] for e in events])


if __name__ == "__main__":
    unittest.main()
