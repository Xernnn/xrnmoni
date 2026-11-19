"""Hyprland: focus changes come from its event socket, and it is found even when the
agent started before it."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

from agent_helpers import AGENT  # noqa: F401

import agent
import platform_linux as pl


class ParseTest(unittest.TestCase):
    def test_event_lines(self):
        self.assertEqual(pl.parse_hypr_event("activewindow>>kitty,notes.txt - vim"),
                         ("window", ("kitty", "notes.txt - vim")))
        self.assertEqual(pl.parse_hypr_event("activewindow>>firefox,a, b, c"),
                         ("window", ("firefox", "a, b, c")))
        self.assertEqual(pl.parse_hypr_event("activewindow>>,"), ("window", (None, None)))
        self.assertEqual(pl.parse_hypr_event("activewindowv2>>55d1c0a3e2b0"),
                         ("address", "55d1c0a3e2b0"))
        self.assertIsNone(pl.parse_hypr_event("workspace>>2"))
        self.assertIsNone(pl.parse_hypr_event("garbage"))

    def test_only_a_focus_change_wakes_the_agent(self):
        woken = []
        ev = pl.HyprEvents("/nowhere", on_change=lambda: woken.append(ev.window))
        ev.feed("activewindow>>kitty,one")
        ev.feed("activewindowv2>>aaa")
        ev.feed("activewindow>>kitty,two")      # the title changed: same window
        ev.feed("activewindowv2>>aaa")
        ev.feed("activewindow>>kitty,shell")    # another terminal
        ev.feed("activewindowv2>>bbb")
        ev.feed("activewindow>>,")              # empty workspace
        self.assertEqual(woken, [("kitty", "one"), ("kitty", "shell"), (None, None)])
        self.assertEqual(ev.window, (None, None))


class FakeHyprland:
    """A Hyprland instance folder with a request socket and an event socket."""

    def __init__(self, runtime, sig="0123abcd_1_1"):
        self.dir = os.path.join(runtime, "hypr", sig)
        os.makedirs(self.dir)
        self.window = {"class": "kitty", "title": "first"}
        self.req = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.req.bind(os.path.join(self.dir, ".socket.sock"))
        self.req.listen(8)
        self.ev = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.ev.bind(os.path.join(self.dir, ".socket2.sock"))
        self.ev.listen(1)
        self.listener = None
        self.requests = []
        threading.Thread(target=self.serve_requests, daemon=True).start()
        threading.Thread(target=self.serve_events, daemon=True).start()

    def serve_requests(self):
        while True:
            try:
                conn, _ = self.req.accept()
            except OSError:
                return
            with conn:
                cmd = conn.recv(1024)
                self.requests.append(cmd)
                conn.sendall(json.dumps(self.window if cmd == b"j/activewindow"
                                        else {"tag": "v0.0.0"}).encode())

    def serve_events(self):
        try:
            self.listener, _ = self.ev.accept()
        except OSError:
            pass

    def send(self, *lines):
        self.listener.sendall("".join(line + "\n" for line in lines).encode())

    def close(self):
        for s in (self.req, self.ev, self.listener):
            if s:
                s.close()


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


@unittest.skipUnless(sys.platform.startswith("linux"), "Unix sockets")
class HyprlandBackendTest(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp()
        saved_env = {k: os.environ.get(k) for k in
                     ("XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY",
                      "WAYLAND_DISPLAY")}
        saved = (pl._MODE, pl._last_detect, dict(pl._HYPR), pl.NAME, dict(pl._settings),
                 pl.REDETECT_SECONDS)
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        for k in ("HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY", "WAYLAND_DISPLAY"):
            os.environ.pop(k, None)   # as for an agent started before Hyprland
        pl._MODE, pl._last_detect, pl.NAME = None, 0.0, "none"
        pl._HYPR.update(instance=None, events=None)

        def restore():
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            pl._MODE, pl._last_detect, hypr, pl.NAME, settings, pl.REDETECT_SECONDS = saved
            pl._HYPR.clear()
            pl._HYPR.update(hypr)
            pl._settings.clear()
            pl._settings.update(settings)
        self.addCleanup(restore)

    def test_hyprland_started_after_the_agent_is_found(self):
        self.assertEqual(pl._detect(), "none")
        self.assertEqual(pl.get_active_window(), (None, None))
        hypr = FakeHyprland(self.runtime)
        self.addCleanup(hypr.close)
        self.assertEqual(pl._detect(), "none")      # not looked for again yet
        pl.REDETECT_SECONDS = 0
        self.assertEqual(pl._detect(), "hyprland")
        self.assertEqual(pl.NAME, "hyprland")
        self.assertEqual(pl._HYPR["instance"], hypr.dir)

    def test_focus_changes_come_from_the_event_socket(self):
        hypr = FakeHyprland(self.runtime)
        self.addCleanup(hypr.close)
        woken = []
        pl._settings["on_change"] = lambda: woken.append(1)
        self.assertEqual(pl._detect(), "hyprland")
        self.assertTrue(wait_for(lambda: pl._HYPR["events"].connected and hypr.listener))
        self.assertEqual(pl.get_active_window(), ("kitty", "first"))
        self.assertIn("event socket", pl.describe())
        asked = len(hypr.requests)
        hypr.send("workspace>>2", "activewindow>>firefox,Example, a page", "activewindowv2>>a1")
        self.assertTrue(wait_for(lambda: pl.get_active_window() == ("firefox", "Example, a page")))
        self.assertTrue(wait_for(lambda: len(woken) >= 2))
        for _ in range(5):
            pl.get_active_window()
        self.assertEqual(len(hypr.requests), asked)  # no polling while events flow

    def test_the_event_socket_is_up_once_detected(self):
        # the banner is printed right after detection and should name what is in use
        hypr = FakeHyprland(self.runtime)
        self.addCleanup(hypr.close)
        self.assertEqual(pl._detect(), "hyprland")
        self.assertTrue(pl._HYPR["events"].connected)
        self.assertTrue(pl.describe().startswith("hyprland (event socket)"))

    def test_a_dead_instance_folder_is_skipped(self):
        os.makedirs(os.path.join(self.runtime, "hypr", "stale_1_1"))
        self.assertIsNone(pl.hypr_instance_dir())
        hypr = FakeHyprland(self.runtime, sig="live_2_2")
        self.addCleanup(hypr.close)
        self.assertEqual(pl.hypr_instance_dir(), hypr.dir)


class BannerTest(unittest.TestCase):
    def test_the_banner_names_the_backend_in_use(self):
        with_describe = types.SimpleNamespace(NAME="none", describe=lambda: "hyprland (x)")
        self.assertEqual(agent.describe_backend(with_describe), "hyprland (x)")
        self.assertEqual(agent.describe_backend(types.SimpleNamespace(NAME="win32")), "win32")


if __name__ == "__main__":
    unittest.main()
