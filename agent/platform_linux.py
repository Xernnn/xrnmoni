"""Linux backend: foreground window + idle time.

Supports two environments, auto-detected:
  * X11        — via python-xlib (EWMH). Idle via `xprintidle` if installed.
  * Hyprland   — via `hyprctl activewindow -j` (Wayland). Idle not available.

Other Wayland compositors (GNOME, KDE) don't expose active-window info to
unprivileged clients without a compositor-specific extension; there we fall
back to "unknown" and log once. See README for notes.
"""
import os
import json
import shutil
import subprocess

NAME = "none"

_MODE = None
_display = None
_atoms = {}


def _init_x11():
    global _display, _atoms, NAME
    from Xlib import display as _d

    _display = _d.Display()
    root = _display.screen().root
    for a in ("_NET_ACTIVE_WINDOW", "_NET_WM_NAME", "_NET_WM_PID", "UTF8_STRING"):
        _atoms[a] = _display.intern_atom(a)
    _atoms["root"] = root
    NAME = "x11"


def _detect():
    global _MODE
    if _MODE is not None:
        return _MODE
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        _MODE = "hyprland"
        globals()["NAME"] = "hyprland"
    elif os.environ.get("DISPLAY"):
        try:
            _init_x11()
            _MODE = "x11"
        except Exception as e:
            print(f"[linux] X11 init failed: {e}", flush=True)
            _MODE = "none"
    else:
        _MODE = "none"
    return _MODE


# ---- X11 -------------------------------------------------------------------
def _x11_active_window():
    from Xlib import X

    root = _atoms["root"]
    prop = root.get_full_property(_atoms["_NET_ACTIVE_WINDOW"], X.AnyPropertyType)
    if not prop or not prop.value:
        return None, None
    wid = prop.value[0]
    if not wid:
        return None, None
    win = _display.create_resource_object("window", wid)

    title = None
    try:
        p = win.get_full_property(_atoms["_NET_WM_NAME"], _atoms["UTF8_STRING"])
        if p and p.value:
            title = p.value.decode("utf-8", "replace")
    except Exception:
        pass
    if title is None:
        try:
            title = win.get_wm_name()
        except Exception:
            title = ""

    app = None
    try:
        cls = win.get_wm_class()  # (instance, class)
        if cls:
            app = cls[1] or cls[0]
    except Exception:
        pass
    if not app:
        try:
            p = win.get_full_property(_atoms["_NET_WM_PID"], 0)
            if p and p.value:
                import psutil
                app = psutil.Process(int(p.value[0])).name()
        except Exception:
            pass
    return (app or "unknown"), (title or "")


def _x11_idle():
    # xprintidle prints idle time in milliseconds
    try:
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=2)
        return max(0.0, int(out.stdout.strip()) / 1000.0)
    except Exception:
        return 0.0


# ---- Hyprland --------------------------------------------------------------
def _hypr_active_window():
    try:
        out = subprocess.run(
            ["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=2
        )
        data = json.loads(out.stdout or "{}")
        if not data:
            return None, None
        return (data.get("class") or "unknown"), (data.get("title") or "")
    except Exception:
        return None, None


# ---- public API ------------------------------------------------------------
def get_active_window():
    mode = _detect()
    if mode == "x11":
        return _x11_active_window()
    if mode == "hyprland":
        return _hypr_active_window()
    return None, None


def get_idle_seconds():
    mode = _detect()
    if mode == "x11" and shutil.which("xprintidle"):
        return _x11_idle()
    # Hyprland / other Wayland: no portable idle source -> treat as active
    return 0.0
