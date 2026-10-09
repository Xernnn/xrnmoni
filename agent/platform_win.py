"""Windows backend: foreground window + idle time.

Requires pywin32 (`pip install pywin32`).
"""
import win32gui
import win32process
import win32api
import psutil

NAME = "win32"


def get_active_window():
    """Return (app_name, window_title) for the foreground window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None, None
    title = win32gui.GetWindowText(hwnd) or ""
    app = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid:
            app = psutil.Process(pid).name()
    except Exception:
        pass
    return app or "unknown", title


def get_idle_seconds():
    """Seconds since the last keyboard/mouse input (whole system)."""
    last = win32api.GetLastInputInfo()
    now = win32api.GetTickCount()
    return max(0.0, (now - last) / 1000.0)
