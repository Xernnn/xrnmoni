"""The Windows backend's logic, run on any OS against a fake Win32 API.

platform_win talks to Windows only through one `_Win32` object, so these tests swap in
`FakeWin32` and check what the agent gets back: UWP apps behind ApplicationFrameHost,
elevated windows, the lock screen, idle time across the 32-bit tick wrap, odd titles, and
that nothing raises. On Windows, `RealWin32Test` also calls the real API once.

    python -m unittest tests.test_platform_win
"""
import os
import signal
import sys
import threading
import time
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

import platform_win  # noqa: E402  (imports on any OS; loads Win32 only when first used)


class FakeWin32:
    def __init__(self):
        self.foreground = 0
        self.windows = {}    # hwnd -> (pid, title, [child hwnds])
        self.images = {}     # pid -> full path, or an exception to raise
        self.snapshot = {}   # pid -> name from the process list, or an exception
        self.aumids = {}     # pid -> AppUserModelID
        self.last_input = 0
        self.ticks = 0
        self.ticks64 = 0
        self.lock = False    # True / False / None, or an exception
        self.raise_on = set()

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise OSError(1, f"{name} failed")

    def foreground_window(self):
        self._maybe_raise("foreground_window")
        return self.foreground

    def window_text(self, hwnd):
        self._maybe_raise("window_text")
        return self.windows[hwnd][1]

    def window_pid(self, hwnd):
        self._maybe_raise("window_pid")
        return self.windows[hwnd][0]

    def child_windows(self, hwnd):
        self._maybe_raise("child_windows")
        return list(self.windows[hwnd][2])

    def process_image(self, pid):
        v = self.images.get(pid)
        if isinstance(v, BaseException):
            raise v
        if v is None:
            raise OSError(87, "The parameter is incorrect")
        return v

    def snapshot_name(self, pid):
        v = self.snapshot.get(pid)
        if isinstance(v, BaseException):
            raise v
        return v

    def process_aumid(self, pid):
        return self.aumids.get(pid)

    def last_input_tick(self):
        self._maybe_raise("last_input_tick")
        return self.last_input

    def tick_count(self):
        return self.ticks

    def tick_count64(self):
        self._maybe_raise("tick_count64")
        return self.ticks64

    def session_locked(self):
        if isinstance(self.lock, BaseException):
            raise self.lock
        return self.lock


def denied():
    return PermissionError(13, "Access is denied")


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeWin32()
        self._old = platform_win._api
        platform_win._api = self.api
        platform_win._warned.clear()

    def tearDown(self):
        platform_win._api = self._old

    def window(self, hwnd, pid, title, exe, children=()):
        self.api.windows[hwnd] = (pid, title, list(children))
        if exe is not None:
            self.api.images[pid] = "C:\\Program Files\\App\\" + exe


class ActiveWindowTest(Base):
    def test_plain_window_reports_exe_name_and_title(self):
        self.window(10, 100, "notes.txt - Notepad", "notepad.exe")
        self.api.foreground = 10
        self.assertEqual(platform_win.get_active_window(), ("notepad.exe", "notes.txt - Notepad"))

    def test_no_foreground_window(self):
        self.api.foreground = 0
        self.assertEqual(platform_win.get_active_window(), (None, None))

    def test_uwp_app_is_named_after_its_own_process_not_the_frame_host(self):
        # Calculator: the top-level window belongs to ApplicationFrameHost.exe; the app's
        # CoreWindow is a child owned by CalculatorApp.exe.
        self.window(20, 200, "Calculator", "ApplicationFrameHost.exe", children=[21, 22])
        self.window(21, 200, "", None)                        # the frame host's own child
        self.window(22, 300, "Calculator", "CalculatorApp.exe")
        self.api.foreground = 20
        self.assertEqual(platform_win.get_active_window(), ("CalculatorApp.exe", "Calculator"))

    def test_uwp_app_in_a_generic_host_is_named_by_its_app_id(self):
        self.window(30, 200, "Some App", "ApplicationFrameHost.exe", children=[31])
        self.window(31, 310, "", "WWAHost.exe")
        self.api.aumids[310] = "Contoso.SomeApp_8wekyb3d8bbwe!App"
        self.api.foreground = 30
        self.assertEqual(platform_win.get_active_window(), ("Contoso.SomeApp", "Some App"))

    def test_frame_host_without_an_app_child_stays_the_frame_host(self):
        self.window(40, 200, "Settings", "ApplicationFrameHost.exe", children=[])
        self.api.foreground = 40
        self.assertEqual(platform_win.get_active_window(), ("ApplicationFrameHost.exe", "Settings"))

    def test_elevated_window_falls_back_to_the_process_list_name(self):
        self.window(50, 500, "Administrator: Terminal", None)
        self.api.images[500] = denied()
        self.api.snapshot[500] = "WindowsTerminal.exe"
        self.api.foreground = 50
        self.assertEqual(platform_win.get_active_window(),
                         ("WindowsTerminal.exe", "Administrator: Terminal"))

    def test_elevated_window_with_no_name_at_all_says_elevated(self):
        self.window(51, 510, "Setup", None)
        self.api.images[510] = denied()
        self.api.snapshot[510] = denied()
        self.api.foreground = 51
        self.assertEqual(platform_win.get_active_window(), ("elevated", "Setup"))

    def test_process_gone_says_unknown(self):
        self.window(52, 520, "Closing", None)   # process_image raises "parameter incorrect"
        self.api.foreground = 52
        self.assertEqual(platform_win.get_active_window(), ("unknown", "Closing"))

    def test_lock_screen_is_no_app(self):
        self.window(60, 600, "Windows Default Lock Screen", "LockApp.exe")
        self.api.foreground = 60
        self.assertEqual(platform_win.get_active_window(), (None, None))

    def test_locked_session_is_no_app_even_if_a_window_is_still_foreground(self):
        self.window(61, 610, "notes.txt - Notepad", "notepad.exe")
        self.api.foreground = 61
        self.api.lock = True
        self.assertEqual(platform_win.get_active_window(), (None, None))

    def test_titles_become_storable_text(self):
        cases = {
            "a\ud83d\ude00b": "a\U0001F600b",      # a pair split by the UTF-16 API is rejoined
            "bad\ud800half": "bad\ufffdhalf",      # a lone half can't go into UTF-8: replaced
            "two\r\nlines\x00": "two lines",       # control characters and NULs
            "x" * 5000: "x" * platform_win.MAX_TITLE,
            None: "",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw[:20] if raw else raw):
                self.window(70, 700, raw, "app.exe")
                self.api.foreground = 70
                app, title = platform_win.get_active_window()
                self.assertEqual(title, want)
                title.encode("utf-8")  # must not raise

    def test_a_failing_call_loses_only_what_it_was_for(self):
        self.window(80, 800, "notes", "app.exe")
        self.window(81, 810, "Settings", "ApplicationFrameHost.exe", children=[])
        cases = [
            ("foreground_window", 80, (None, None)),
            ("window_text", 80, ("app.exe", "")),
            ("window_pid", 80, ("unknown", "notes")),
            ("child_windows", 81, ("ApplicationFrameHost.exe", "Settings")),
        ]
        for failing, hwnd, want in cases:
            with self.subTest(failing=failing):
                self.api.foreground = hwnd
                self.api.raise_on = {failing}
                self.assertEqual(platform_win.get_active_window(), want)

    def test_a_failing_lock_check_does_not_stop_app_tracking(self):
        self.window(82, 820, "notes", "app.exe")
        self.api.foreground = 82
        self.api.lock = OSError(5, "WTS failed")
        self.assertEqual(platform_win.get_active_window(), ("app.exe", "notes"))

    def test_api_that_cannot_load_reports_nothing(self):
        def broken():
            raise OSError("no user32 here")
        platform_win._api = None
        old = platform_win._Win32
        platform_win._Win32 = broken
        try:
            self.assertEqual(platform_win.get_active_window(), (None, None))
            self.assertEqual(platform_win.get_idle_seconds(), 0.0)
            self.assertIsNone(platform_win.is_locked())
        finally:
            platform_win._Win32 = old


class IdleTest(Base):
    def test_idle_seconds(self):
        self.api.last_input, self.api.ticks = 1_000, 91_000
        self.assertEqual(platform_win.get_idle_seconds(), 90.0)

    def test_tick_count_wraps_after_49_days(self):
        self.api.last_input = 0xFFFFFFFF - 999   # 1 s before the wrap
        self.api.ticks = 4_000                    # 4 s after it
        self.assertEqual(platform_win.get_idle_seconds(), 5.0)

    def test_input_stamped_just_after_now_is_no_idle_not_49_days(self):
        self.api.last_input, self.api.ticks = 10_016, 10_000
        self.assertEqual(platform_win.get_idle_seconds(), 0.0)

    def test_failure_is_zero_idle(self):
        self.api.raise_on = {"last_input_tick"}
        self.assertEqual(platform_win.get_idle_seconds(), 0.0)


class LockTest(Base):
    def test_session_lock_state(self):
        self.api.foreground = 0
        for state, want in ((True, True), (False, False)):
            with self.subTest(state=state):
                self.api.lock = state
                self.assertIs(platform_win.is_locked(), want)

    def test_lock_app_in_front_counts_as_locked_when_the_session_cannot_tell(self):
        self.window(60, 600, "", "LockApp.exe")
        self.api.foreground = 60
        self.api.lock = None
        self.assertIs(platform_win.is_locked(), True)

    def test_unknown_when_nothing_can_tell(self):
        self.window(61, 610, "notes", "notepad.exe")
        self.api.foreground = 61
        self.api.lock = OSError(5, "WTS failed")
        self.assertIsNone(platform_win.is_locked())


class SessionEndTest(unittest.TestCase):
    """The hidden window's decisions: Windows ends the session (or taskkill asks politely),
    the agent is told once and given a few seconds to write what it has."""

    def make(self, exited=True):
        calls = []
        done = threading.Event()
        if exited:
            done.set()
        watch = platform_win._SessionEnd(lambda reason: calls.append(reason), done, wait=0.2)
        return watch, calls

    def test_query_end_session_says_yes_and_does_nothing_yet(self):
        watch, calls = self.make()
        self.assertEqual(watch.on_message(platform_win.WM_QUERYENDSESSION, 0), 1)
        self.assertEqual(calls, [])

    def test_end_session_stops_the_agent_and_waits_for_it(self):
        watch, calls = self.make(exited=False)
        self.assertEqual(watch.on_message(platform_win.WM_ENDSESSION, 1), 0)
        self.assertEqual(calls, ["session end"])

    def test_cancelled_end_session_keeps_running(self):
        watch, calls = self.make()
        watch.on_message(platform_win.WM_ENDSESSION, 0)
        self.assertEqual(calls, [])

    def test_close_from_taskkill_stops_once(self):
        watch, calls = self.make()
        self.assertEqual(watch.on_message(platform_win.WM_CLOSE, 0), 0)
        watch.on_message(platform_win.WM_CLOSE, 0)
        watch.on_message(platform_win.WM_ENDSESSION, 1)
        self.assertEqual(calls, ["close"])

    def test_other_messages_go_to_the_default_handler(self):
        watch, calls = self.make()
        self.assertIsNone(watch.on_message(0x0001, 0))  # WM_CREATE
        self.assertEqual(calls, [])

    def test_console_close_logoff_and_shutdown_stop_the_agent(self):
        for event in (platform_win.CTRL_CLOSE_EVENT, platform_win.CTRL_LOGOFF_EVENT,
                      platform_win.CTRL_SHUTDOWN_EVENT):
            with self.subTest(event=event):
                watch, calls = self.make()
                self.assertTrue(watch.on_console(event))
                self.assertEqual(len(calls), 1)

    def test_ctrl_c_is_left_to_python(self):
        watch, calls = self.make()
        self.assertFalse(watch.on_console(platform_win.CTRL_C_EVENT))
        self.assertEqual(calls, [])


class InterruptMainTest(unittest.TestCase):
    """The default stop goes through the agent's own signal handling."""

    def run_in_thread(self, fn):
        t = threading.Thread(target=fn)
        t.start()
        t.join()

    def test_uses_the_agents_sigterm_handler_when_it_has_one(self):
        got = []
        old = signal.signal(signal.SIGTERM, lambda s, f: got.append(s))
        try:
            self.run_in_thread(lambda: platform_win._interrupt_main("test"))
            for _ in range(100):           # the handler runs in this (main) thread
                if got:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(got, [signal.SIGTERM])
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_falls_back_to_keyboard_interrupt(self):
        old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_in_thread(lambda: platform_win._interrupt_main("test"))
                for _ in range(100):
                    threading.Event().wait(0.01)
        finally:
            signal.signal(signal.SIGTERM, old)


class NoPywin32Test(unittest.TestCase):
    def test_backend_needs_only_ctypes(self):
        # pywin32 was installed on every PC (and into the exe) for three calls; ctypes has them.
        with open(platform_win.__file__, encoding="utf-8") as f:
            self.assertNotRegex(f.read(), r"(?m)^\s*(import|from)\s+(win32\w*|pywintypes)\b")
        with open(os.path.join(ROOT, "agent", "requirements.txt"), encoding="utf-8") as f:
            self.assertNotIn("pywin32", f.read())


class ElapsedTest(Base):
    def test_elapsed_seconds_is_the_64_bit_tick_count(self):
        self.api.ticks64 = 50 * 86400 * 1000 + 500   # past where GetTickCount wraps
        self.assertEqual(50 * 86400 + 0.5, platform_win.elapsed_seconds())

    def test_elapsed_seconds_never_raises(self):
        self.api.raise_on.add("tick_count64")
        self.assertIsInstance(platform_win.elapsed_seconds(), float)


class BootTimeTest(Base):
    def test_boot_time_is_the_clock_minus_the_64_bit_tick_count(self):
        # psutil's is the clock minus the uptime in whole seconds, so it moved by up to a
        # second from one start of the agent to the next.
        self.api.ticks64 = 3 * 86400 * 1000 + 250   # 3 days, 0.25 s: past the 32-bit wrap
        with unittest.mock.patch.object(platform_win.time, "time", return_value=1.7e9):
            self.assertAlmostEqual(1.7e9 - 3 * 86400 - 0.25, platform_win.boot_time(), places=3)

    def test_boot_time_falls_back_to_psutil(self):
        self.api.raise_on.add("tick_count64")
        with unittest.mock.patch("psutil.boot_time", return_value=1.6e9):
            self.assertEqual(1.6e9, platform_win.boot_time())


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealWin32Test(unittest.TestCase):
    """One pass over the real API (CI's windows-latest job): right types, no exceptions."""

    def setUp(self):
        platform_win._api = None

    def test_real_calls(self):
        app, title = platform_win.get_active_window()
        self.assertTrue(app is None or isinstance(app, str))
        self.assertTrue(title is None or isinstance(title, str))
        idle = platform_win.get_idle_seconds()
        self.assertIsInstance(idle, float)
        self.assertGreaterEqual(idle, 0.0)
        self.assertIn(platform_win.is_locked(), (True, False, None))
        import psutil
        self.assertLess(abs(platform_win.boot_time() - psutil.boot_time()), 5)
        self.assertGreater(platform_win.elapsed_seconds(), 0.0)
        self.assertLess(abs(platform_win.boot_time() + platform_win.elapsed_seconds() - time.time()), 1)
        print(f"\n[real win32] app={app!r} title={title!r} idle={idle} locked={platform_win.is_locked()}")

    def test_own_process_name(self):
        name = platform_win._process_name(platform_win._get_api(), os.getpid())
        self.assertTrue(name.lower().startswith("python"), name)

    def test_hidden_window_and_single_instance(self):
        self.assertTrue(platform_win.start_session_watch(on_end=lambda reason: None))
        name = f"xrnmoni-test-{os.getpid()}"
        self.assertTrue(platform_win.claim_single_instance(name))
        self.assertFalse(platform_win.claim_single_instance(name))  # a second claim, as a second agent


if __name__ == "__main__":
    unittest.main()
