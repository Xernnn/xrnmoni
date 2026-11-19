"""Windows backend: foreground window, idle time and lock state.

Talks to user32, kernel32 and wtsapi32 through ctypes, and only through one `_Win32` object
(`_api`), so the logic below can be tested on any OS with a fake one
(tests/test_platform_win.py). The public functions never raise: a call that fails is
logged once and reported as "don't know" (None / 0.0).

Also here, for windows/launcher.py: a hidden window that tells the agent to stop and write
what it has when Windows ends the session (or `taskkill /pid` asks it to close), and a
per-session mutex so a second copy of the agent doesn't count the same time twice.
"""
import _thread
import atexit
import ctypes
import logging
import ntpath
import re
import signal
import threading

NAME = "win32"
log = logging.getLogger("xrnmoni.platform_win")

FRAME_HOST = "applicationframehost.exe"   # hosts the window frame of every UWP app
LOCK_APPS = {"lockapp.exe"}               # the lock screen
GENERIC_HOSTS = {"wwahost.exe"}           # runs JavaScript UWP apps: the app id names the app
MAX_TITLE = 1024

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT = 0, 1, 2
CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 5, 6

_api = None
_warned = set()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _get_api():
    global _api
    if _api is None:
        _api = _Win32()
    return _api


def _warn_once(what, err):
    key = (what, type(err).__name__, str(err))
    if key not in _warned:
        _warned.add(key)
        log.warning("%s failed: %s", what, err)


def _clean(text):
    """Window titles as text that can be stored: GetWindowTextW is UTF-16, and a title can
    hold half of a surrogate pair (or both halves as separate code points), which UTF-8,
    JSON consumers and SQLite refuse. Rejoin pairs, replace lone halves, drop control
    characters and cap the length."""
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = str(text).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return _CONTROL.sub(" ", text).strip()[:MAX_TITLE]


def _is_denied(err):
    return isinstance(err, PermissionError) or getattr(err, "winerror", None) == ERROR_ACCESS_DENIED


def _process_name(api, pid):
    """The exe name of a process. An elevated (or protected) process still answers
    QueryFullProcessImageNameW with limited rights; if even that is refused, the process
    list has its name; failing both, "elevated" (refused) or "unknown" (gone)."""
    refused = False
    try:
        return ntpath.basename(api.process_image(pid)) or "unknown"
    except Exception as e:
        refused = _is_denied(e)
    try:
        name = api.snapshot_name(pid)
        if name:
            return name
    except Exception as e:
        refused = refused or _is_denied(e)
    return "elevated" if refused else "unknown"


def _uwp_app_pid(api, hwnd, host_pid):
    """A UWP app's top-level window belongs to ApplicationFrameHost.exe; the app itself owns
    a child window (its CoreWindow). Return that child's process, if there is one."""
    try:
        children = api.child_windows(hwnd)
    except Exception as e:
        _warn_once("child windows", e)
        return None
    for child in children:
        try:
            pid = api.window_pid(child)
        except Exception:
            continue
        if pid and pid != host_pid:
            return pid
    return None


def _app_name(api, hwnd, pid):
    name = _process_name(api, pid)
    if name.lower() == FRAME_HOST:
        app_pid = _uwp_app_pid(api, hwnd, pid)
        if app_pid:
            pid, name = app_pid, _process_name(api, app_pid)
    if name.lower() in GENERIC_HOSTS:
        aumid = api.process_aumid(pid)   # "Contoso.SomeApp_8wekyb3d8bbwe!App"
        if aumid:
            name = aumid.split("!")[0].rsplit("_", 1)[0] or name
    return name


def _session_locked(api):
    try:
        return api.session_locked()
    except Exception as e:
        _warn_once("session lock state", e)
        return None


def get_active_window():
    """Return (app_name, window_title) for the foreground window, or (None, None) when no
    app is in front: no foreground window, the lock screen, or a locked session."""
    try:
        api = _get_api()
    except Exception as e:
        _warn_once("Win32 API", e)
        return None, None
    if _session_locked(api):
        return None, None
    try:
        hwnd = api.foreground_window()
    except Exception as e:
        _warn_once("foreground window", e)
        return None, None
    if not hwnd:
        return None, None
    try:
        pid = api.window_pid(hwnd)
        app = _app_name(api, hwnd, pid) if pid else "unknown"
    except Exception as e:
        _warn_once("window process", e)
        app = "unknown"
    if app.lower() in LOCK_APPS:
        return None, None
    try:
        title = _clean(api.window_text(hwnd))
    except Exception as e:
        _warn_once("window title", e)
        title = ""
    return app, title


def get_idle_seconds():
    """Seconds since the last keyboard/mouse input in this session."""
    try:
        api = _get_api()
        last = api.last_input_tick()
        now = api.tick_count()
    except Exception as e:
        _warn_once("idle time", e)
        return 0.0
    # Both are 32-bit millisecond tick counts that wrap every 49.7 days, so subtract modulo
    # 2**32. A "negative" result (input stamped a tick after we read the clock) is no idle.
    diff = (now - last) & 0xFFFFFFFF
    if diff >= 0x80000000:
        diff = 0
    return diff / 1000.0


def is_locked():
    """True when the session is locked (or switched away from), False when it is in use,
    None when Windows can't tell us."""
    try:
        api = _get_api()
    except Exception as e:
        _warn_once("Win32 API", e)
        return None
    state = _session_locked(api)
    if state is not None:
        return bool(state)
    try:
        hwnd = api.foreground_window()
        if hwnd:
            pid = api.window_pid(hwnd)
            if pid and _process_name(api, pid).lower() in LOCK_APPS:
                return True
    except Exception as e:
        _warn_once("lock screen check", e)
    return None


# ---- stopping cleanly when the session ends ----------------------------------------------

def _interrupt_main(reason):
    """Stop the agent the way Ctrl-C or SIGTERM would: its own handler (SIGTERM if it
    installed one, else KeyboardInterrupt) runs in the main thread and writes what it has.
    Windows sends no signals to a windowed process, so this is the only way to reach it."""
    log.info("stopping: %s", reason)
    sig = signal.SIGTERM if callable(signal.getsignal(signal.SIGTERM)) else signal.SIGINT
    try:
        _thread.interrupt_main(sig)
    except TypeError:  # Python < 3.10 takes no signal number
        _thread.interrupt_main()


class _SessionEnd:
    """What the hidden window and the console handler do with each message, kept apart
    from the Win32 plumbing so it can be tested anywhere."""

    def __init__(self, on_end, exited, wait=5.0):
        self.on_end = on_end
        self.exited = exited
        self.wait = wait
        self._stopping = False
        self._lock = threading.Lock()

    def _stop(self, reason):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        try:
            self.on_end(reason)
        except Exception as e:
            log.warning("stop handler failed: %s", e)

    def on_message(self, msg, wparam):
        """Return the window procedure's result, or None for DefWindowProc."""
        if msg == WM_QUERYENDSESSION:
            return 1      # yes, the session may end; WM_ENDSESSION follows if it does
        if msg == WM_ENDSESSION:
            if wparam:    # it is ending: once we return, Windows may end the process
                self._stop("session end")
                self.exited.wait(self.wait)
            return 0
        if msg == WM_CLOSE:   # taskkill /pid (without /f), windows/stop.ps1
            self._stop("close")
            return 0
        return None

    def on_console(self, event):
        """Console control events (only when run from a console). Ctrl-C and Ctrl-Break are
        Python's own; closing the console, logoff and shutdown end the process as soon as
        this returns, so stop the agent and wait for it first."""
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            self._stop("console closed" if event == CTRL_CLOSE_EVENT else "session end")
            self.exited.wait(self.wait)
            return True
        return False


_exited = threading.Event()
atexit.register(_exited.set)
_watching = False
_keep = []  # ctypes callbacks and handles that must outlive the call that made them


def start_session_watch(on_end=_interrupt_main, wait=5.0):
    """Create the hidden top-level window (message-only windows don't get WM_ENDSESSION) on
    a thread of its own, and a console handler if there is a console. Returns True when the
    window is up."""
    global _watching
    if _watching:   # one window per process: the class keeps the first window procedure
        return True
    watch = _SessionEnd(on_end, _exited, wait)
    try:
        _watching = _get_api().start_session_watch(watch)
    except Exception as e:
        _warn_once("session watch", e)
    return _watching


def claim_single_instance(name):
    """True if no other process in this logon session holds `name`; keeps holding it."""
    try:
        return _get_api().claim_mutex("Local\\" + name)
    except Exception as e:
        _warn_once("single-instance mutex", e)
        return True


# ---- the real Win32 API ------------------------------------------------------------------

class _Win32:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WTS_CURRENT_SESSION = 0xFFFFFFFF
    WTSSessionInfoEx = 25
    WTS_SESSIONSTATE_LOCK = 0
    WTS_SESSIONSTATE_UNLOCK = 1
    WTSDisconnected = 4

    def __init__(self):
        from ctypes import wintypes as w

        self.w = w
        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wtsapi32 = t = ctypes.WinDLL("wtsapi32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t

        def proto(fn, restype, *argtypes):
            fn.restype, fn.argtypes = restype, list(argtypes)

        self.ENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.CTRLPROC = ctypes.WINFUNCTYPE(w.BOOL, w.DWORD)

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

        class WTSINFOEX_LEVEL1_W(ctypes.Structure):
            _fields_ = [("SessionId", w.ULONG), ("SessionState", ctypes.c_int),
                        ("SessionFlags", w.LONG), ("WinStationName", w.WCHAR * 33),
                        ("UserName", w.WCHAR * 21), ("DomainName", w.WCHAR * 18),
                        ("LogonTime", ctypes.c_longlong), ("ConnectTime", ctypes.c_longlong),
                        ("DisconnectTime", ctypes.c_longlong),
                        ("LastInputTime", ctypes.c_longlong), ("CurrentTime", ctypes.c_longlong),
                        ("IncomingBytes", w.DWORD), ("OutgoingBytes", w.DWORD),
                        ("IncomingFrames", w.DWORD), ("OutgoingFrames", w.DWORD),
                        ("IncomingCompressedBytes", w.DWORD), ("OutgoingCompressedBytes", w.DWORD)]

        class WTSINFOEXW(ctypes.Structure):
            _fields_ = [("Level", w.DWORD), ("Data", WTSINFOEX_LEVEL1_W)]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                        ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR)]

        self.LASTINPUTINFO, self.WTSINFOEXW, self.WNDCLASSW = LASTINPUTINFO, WTSINFOEXW, WNDCLASSW

        proto(u.GetForegroundWindow, w.HWND)
        proto(u.GetWindowTextLengthW, ctypes.c_int, w.HWND)
        proto(u.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        proto(u.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        proto(u.EnumChildWindows, w.BOOL, w.HWND, self.ENUMPROC, w.LPARAM)
        proto(u.GetLastInputInfo, w.BOOL, ctypes.POINTER(LASTINPUTINFO))
        proto(u.RegisterClassW, w.ATOM, ctypes.POINTER(WNDCLASSW))
        proto(u.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
              ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
        proto(u.DefWindowProcW, LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        proto(u.GetMessageW, w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
        proto(u.TranslateMessage, w.BOOL, ctypes.POINTER(w.MSG))
        proto(u.DispatchMessageW, LRESULT, ctypes.POINTER(w.MSG))
        proto(k.GetTickCount, w.DWORD)
        proto(k.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        proto(k.CloseHandle, w.BOOL, w.HANDLE)
        proto(k.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR,
              ctypes.POINTER(w.DWORD))
        proto(k.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
        proto(k.GetConsoleWindow, w.HWND)
        proto(k.SetConsoleCtrlHandler, w.BOOL, self.CTRLPROC, w.BOOL)
        proto(k.CreateMutexW, w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
        self.GetApplicationUserModelId = getattr(k, "GetApplicationUserModelId", None)  # Win8+
        if self.GetApplicationUserModelId:
            proto(self.GetApplicationUserModelId, w.LONG, w.HANDLE,
                  ctypes.POINTER(ctypes.c_uint32), w.LPWSTR)
        proto(t.WTSQuerySessionInformationW, w.BOOL, w.HANDLE, w.DWORD, ctypes.c_int,
              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD))
        proto(t.WTSFreeMemory, None, ctypes.c_void_p)

    def _error(self):
        return ctypes.WinError(ctypes.get_last_error())

    # windows
    def foreground_window(self):
        return self.user32.GetForegroundWindow() or 0

    def window_text(self, hwnd):
        n = self.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        got = self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf[:max(got, 0)]  # by length, not .value, so a NUL inside doesn't cut it short

    def window_pid(self, hwnd):
        pid = self.w.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def child_windows(self, hwnd):
        found = []

        def add(child, _):
            found.append(child)
            return True

        self.user32.EnumChildWindows(hwnd, self.ENUMPROC(add), 0)
        return found

    # processes
    def _open(self, pid):
        h = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            raise self._error()
        return h

    def process_image(self, pid):
        h = self._open(pid)
        try:
            size = self.w.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                raise self._error()
            return buf.value
        finally:
            self.kernel32.CloseHandle(h)

    def snapshot_name(self, pid):
        import psutil  # the process list: names even processes we may not open
        return psutil.Process(pid).name()

    def process_aumid(self, pid):
        if not self.GetApplicationUserModelId:
            return None
        try:
            h = self._open(pid)
        except OSError:
            return None
        try:
            n = ctypes.c_uint32(256)
            buf = ctypes.create_unicode_buffer(n.value)
            if self.GetApplicationUserModelId(h, ctypes.byref(n), buf) != 0:
                return None  # APPMODEL_ERROR_NO_APPLICATION: not a packaged app
            return buf.value
        finally:
            self.kernel32.CloseHandle(h)

    # input and session
    def last_input_tick(self):
        info = self.LASTINPUTINFO(cbSize=ctypes.sizeof(self.LASTINPUTINFO))
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            raise self._error()
        return info.dwTime

    def tick_count(self):
        return self.kernel32.GetTickCount()

    def session_locked(self):
        buf, size = ctypes.c_void_p(), self.w.DWORD()
        if not self.wtsapi32.WTSQuerySessionInformationW(
                None, self.WTS_CURRENT_SESSION, self.WTSSessionInfoEx,
                ctypes.byref(buf), ctypes.byref(size)):
            raise self._error()
        try:
            info = ctypes.cast(buf, ctypes.POINTER(self.WTSINFOEXW)).contents
            if info.Level != 1:
                return None
            data = info.Data
            if data.SessionState == self.WTSDisconnected:   # another user switched in
                return True
            # (Windows 7 reported these two the wrong way round; 10 and 11 don't.)
            if data.SessionFlags == self.WTS_SESSIONSTATE_LOCK:
                return True
            if data.SessionFlags == self.WTS_SESSIONSTATE_UNLOCK:
                return False
            return None
        finally:
            self.wtsapi32.WTSFreeMemory(buf)

    # the hidden window
    def start_session_watch(self, watch):
        ready = threading.Event()
        result = []

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                r = watch.on_message(msg, wparam)
            except Exception:
                r = None
            if r is None:
                return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            return r

        def run():
            proc = self.WNDPROC(wndproc)
            _keep.append(proc)
            hinst = self.kernel32.GetModuleHandleW(None)
            cls = self.WNDCLASSW(lpfnWndProc=proc, hInstance=hinst,
                                 lpszClassName="xrnmoni-session-watch")
            self.user32.RegisterClassW(ctypes.byref(cls))
            hwnd = self.user32.CreateWindowExW(0, "xrnmoni-session-watch", "xrnmoni", 0,
                                               0, 0, 0, 0, None, None, hinst, None)
            result.append(bool(hwnd))
            if not hwnd:
                log.warning("hidden window not created: %s", self._error())
            ready.set()
            if not hwnd:
                return
            msg = self.w.MSG()
            while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))

        threading.Thread(target=run, name="xrnmoni-session-watch", daemon=True).start()
        ready.wait(5)

        if self.kernel32.GetConsoleWindow():
            handler = self.CTRLPROC(lambda event: bool(watch.on_console(event)))
            _keep.append(handler)
            self.kernel32.SetConsoleCtrlHandler(handler, True)
        return bool(result and result[0])

    def claim_mutex(self, name):
        h = self.kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            raise self._error()
        _keep.append(h)
        return err != ERROR_ALREADY_EXISTS
