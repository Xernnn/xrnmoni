"""Locked screens and sleep become their own intervals instead of app or idle time."""
import sys
import textwrap
import time
import unittest

from agent_helpers import config

import agent
import platform_linux as pl
from test_tracker import FakeBackend, T0, Clock

SESSION = "/org/freedesktop/login1/session/_2"
LOCK = (SESSION + ": org.freedesktop.DBus.Properties.PropertiesChanged "
        "('org.freedesktop.login1.Session', {'LockedHint': <%s>}, @as [])")
SLEEP = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)"


class LogindParseTest(unittest.TestCase):
    def test_gdbus_monitor_lines(self):
        self.assertEqual(pl.parse_logind_signal(SLEEP % "true", SESSION), ("sleep", True))
        self.assertEqual(pl.parse_logind_signal(SLEEP % "false", SESSION), ("sleep", False))
        self.assertEqual(pl.parse_logind_signal(LOCK % "true", SESSION), ("locked", True))
        self.assertEqual(pl.parse_logind_signal(LOCK % "false", SESSION), ("locked", False))
        other = LOCK.replace("_2", "_9") % "true"  # someone else's session
        self.assertIsNone(pl.parse_logind_signal(other, SESSION))
        idle = (SESSION + ": org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.login1.Session', {'IdleHint': <true>}, @as [])")
        self.assertIsNone(pl.parse_logind_signal(idle, SESSION))
        self.assertIsNone(pl.parse_logind_signal("Monitoring signals from all objects", SESSION))


class LogindWatcherTest(unittest.TestCase):
    def setUp(self):
        saved = pl._busctl
        pl._busctl = lambda *a: [False]
        self.addCleanup(setattr, pl, "_busctl", saved)

    def test_it_follows_a_gdbus_monitor_stream(self):
        lines = ["Monitoring signals from all objects owned by org.freedesktop.login1",
                 LOCK % "true", SLEEP % "true"]
        script = textwrap.dedent(f"""
            import sys, time
            for line in {lines!r}:
                print(line, flush=True)
            time.sleep(30)
        """)
        changes = []
        w = pl.LogindWatcher(SESSION, on_change=lambda: changes.append(1),
                             command=[sys.executable, "-c", script])
        w.start()
        self.addCleanup(w.stop)
        end = time.time() + 5
        while time.time() < end and len(changes) < 2:
            time.sleep(0.02)
        self.assertTrue(w.locked)
        self.assertIsNotNone(w.sleep_since)
        w.feed(SLEEP % "false")
        self.assertIsNone(w.sleep_since)

    def test_is_locked_combines_logind_and_hyprland(self):
        class W:
            locked = False

        class C:
            locked = None
        saved = dict(pl._LOGIND), dict(pl._IDLE)

        def restore():
            pl._LOGIND.clear()
            pl._LOGIND.update(saved[0])
            pl._IDLE.clear()
            pl._IDLE.update(saved[1])
        self.addCleanup(restore)
        pl._LOGIND.update(started=True, watcher=None)
        pl._IDLE.clear()
        self.assertIsNone(pl.is_locked())          # nothing can tell
        pl._IDLE["client"] = C()
        self.assertIsNone(pl.is_locked())          # compositor without lock-notify
        C.locked = False
        self.assertIs(pl.is_locked(), False)
        C.locked = True
        self.assertIs(pl.is_locked(), True)        # Hyprland says locked
        C.locked = False
        pl._LOGIND["watcher"] = W()
        W.locked = True
        self.assertIs(pl.is_locked(), True)        # logind says locked


class LockSleepBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.locked = False
        self.sleeping = None

    def is_locked(self):
        return self.locked

    def sleep_started(self):
        return self.sleeping


class TrackerLockSleepTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.backend = LockSleepBackend()
        self.clock = Clock(T0)
        self.elapsed = Clock(1000.0)
        self.tracker = agent.Tracker(config(poll_interval=2.0), self.events.extend,
                                     self.backend, now=self.clock, clock=self.elapsed)

    def run_for(self, seconds, step=2.0):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.tracker.tick()
            self.clock.t += step
            self.elapsed.t += step

    def total(self, kind):
        return sum(e["ts_end"] - e["ts_start"] for e in self.events if e["type"] == kind)

    def test_a_locked_screen_is_locked_time(self):
        self.run_for(10)
        self.backend.locked = True
        self.backend.idle = 120.0      # the lock screen is idle too: locked wins
        self.run_for(150)
        self.backend.locked = False
        self.backend.idle = 0.0
        self.run_for(10)
        self.tracker.close()
        self.assertEqual((self.total("app"), self.total("locked"), self.total("idle")),
                         (20, 150, 0))
        self.assertTrue(all(e["ts_end"] - e["ts_start"] <= 60 for e in self.events))
        locked = [e for e in self.events if e["type"] == "locked"]
        self.assertTrue(all("app" not in e for e in locked))

    def test_sleep_starts_when_logind_announces_it(self):
        self.run_for(10)               # ticks at +0 .. +8
        self.backend.sleeping = T0 + 9
        self.tracker.tick()            # woken by PrepareForSleep at +10
        self.clock.t += 600            # suspended, the process frozen
        self.elapsed.t += 600
        self.tracker.tick()            # thawed: logind hasn't said "back" yet
        self.backend.sleeping = None
        self.clock.t += 1
        self.elapsed.t += 1
        self.run_for(10)
        self.tracker.close()
        apps = [(e["ts_start"] - T0, e["ts_end"] - T0) for e in self.events
                if e["type"] == "app"]
        self.assertEqual(apps[0], (0, 9))
        sleep = [e for e in self.events if e["type"] == "sleep"]
        self.assertEqual((sleep[0]["ts_start"] - T0, sleep[-1]["ts_end"] - T0), (9, 611))
        self.assertEqual(self.total("sleep"), 602)  # one sleep, not one per signal and gap


if __name__ == "__main__":
    unittest.main()
