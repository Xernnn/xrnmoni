"""Linux backend: foreground window, idle time, lock and sleep.

Supports two environments, auto-detected:
  * X11        — via python-xlib (EWMH). Idle via `xprintidle` if installed.
  * Hyprland   — via its IPC sockets (Wayland): focus changes arrive as events on
                 .socket2.sock, so even a one-second switch is seen; .socket.sock is
                 asked directly when the event socket is down. An agent started before
                 Hyprland (no HYPRLAND_INSTANCE_SIGNATURE yet) finds it later.

Idle, first that works: the Wayland ext-idle-notify-v1 protocol (Hyprland, Sway,
KDE...; a small client below speaks the wire protocol, no dependencies),
`xprintidle` on X11, logind's IdleHint, else none. Which one is in use is logged.

Lock: logind's LockedHint, and on Hyprland its lock-notify protocol (any
ext-session-lock locker: hyprlock, Quickshell, swaylock). Sleep: logind's
PrepareForSleep. logind is watched with `gdbus monitor`.

Other Wayland compositors (GNOME, KDE) don't expose active-window info to
unprivileged clients without a compositor-specific extension; there we fall
back to "unknown" and log once. See README for notes.
"""
import atexit
import os
import json
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

NAME = "none"

_MODE = None
_last_detect = 0.0
REDETECT_SECONDS = 5.0
_display = None
_atoms = {}
_HYPR = {"instance": None, "events": None}


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
    """Which window backend to use. "none" is retried every REDETECT_SECONDS, so an
    agent started before the desktop picks it up once it's there."""
    global _MODE, _last_detect
    if _MODE not in (None, "none"):
        return _MODE
    if _MODE == "none" and time.time() - _last_detect < REDETECT_SECONDS:
        return _MODE
    _last_detect = time.time()
    instance = hypr_instance_dir()
    if instance:
        _HYPR["instance"] = instance
        _HYPR["events"] = HyprEvents(instance)
        _HYPR["events"].start()
        _MODE = "hyprland"
        globals()["NAME"] = "hyprland"
    elif os.environ.get("DISPLAY"):
        try:
            _init_x11()
            _MODE = "x11"
        except Exception as e:
            if _MODE is None:
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
def _runtime_dir(env=os.environ):
    return env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def _hypr_request(instance, command):
    """Ask Hyprland's request socket (what hyprctl does), e.g. b"j/activewindow"."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(os.path.join(instance, ".socket.sock"))
        s.sendall(command)
        out = b""
        while True:
            data = s.recv(65536)
            if not data:
                return out
            out += data


def hypr_instance_dir(env=os.environ):
    """The running Hyprland's socket folder: $HYPRLAND_INSTANCE_SIGNATURE's, else the
    newest live one under $XDG_RUNTIME_DIR/hypr (the environment of an agent started
    before Hyprland, or across a Hyprland restart, doesn't name it)."""
    base = os.path.join(_runtime_dir(env), "hypr")
    found = []
    try:
        found = sorted((os.path.join(base, d) for d in os.listdir(base)),
                       key=lambda d: os.path.getmtime(d), reverse=True)
    except OSError:
        pass
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        found.insert(0, os.path.join(base, sig))
    for d in found:
        try:
            _hypr_request(d, b"j/version")
            return d
        except OSError:
            continue
    return None


def _hypr_active_window(instance=None):
    try:
        data = json.loads(_hypr_request(instance or _HYPR["instance"], b"j/activewindow")
                          or b"{}")
        if not data:
            return None, None
        return (data.get("class") or "unknown"), (data.get("title") or "")
    except Exception:
        return None, None


def parse_hypr_event(line):
    """A .socket2.sock line -> ("window", (class, title)) | ("address", addr) | None."""
    event, sep, data = line.partition(">>")
    if not sep:
        return None
    if event == "activewindow":
        cls, _, title = data.partition(",")
        if not cls and not title:
            return "window", (None, None)
        return "window", (cls or "unknown", title)
    if event == "activewindowv2":
        return "address", data
    return None


class HyprEvents:
    """Follows Hyprland's event socket for the focused window.

    Hyprland sends activewindow (class, title) and then activewindowv2 (address) when
    focus moves and when the focused window's title changes. Only a new address wakes
    the agent at once; a title change waits for the next regular sample,
    so a terminal animating its title doesn't make an interval per frame."""

    def __init__(self, instance, on_change=None):
        self.instance = instance
        self.on_change = on_change
        self.window = (None, None)
        self.address = None
        self.connected = False
        self._tried = threading.Event()

    def _notify(self):
        callback = self.on_change or _settings["on_change"]
        if callback:
            callback()

    def feed(self, line):
        got = parse_hypr_event(line)
        if not got:
            return
        kind, value = got
        if kind == "window":
            self.window = value
            if value == (None, None):
                self.address = None
                self._notify()
        elif value != self.address:
            self.address = value
            self._notify()

    def run(self):
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(os.path.join(self.instance, ".socket2.sock"))
                self.window = _hypr_active_window(self.instance)  # focus before we listened
                self.connected = True
                self._tried.set()
                self._notify()
                buf = b""
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    *lines, buf = (buf + data).split(b"\n")
                    for line in lines:
                        self.feed(line.decode("utf-8", "replace"))
            except OSError:
                pass
            finally:
                sock.close()
            self.connected = False
            self._tried.set()
            time.sleep(2)
            # Hyprland restarted: it has a new instance signature
            self.instance = hypr_instance_dir() or self.instance
            _HYPR["instance"] = self.instance

    def start(self, wait=1.0):
        """Start listening; wait up to `wait` s for the first connection, so what the
        agent reports at start (and its banner) is already the event socket."""
        threading.Thread(target=self.run, daemon=True, name="hyprland-events").start()
        self._tried.wait(wait)


# ---- Wayland wire protocol -------------------------------------------------
# Just enough of it to bind wl_seat and ext_idle_notifier_v1 and hear "idled" /
# "resumed". Messages are: object id (u32), size << 16 | opcode (u32), arguments;
# a string is its length with the NUL (u32), the bytes, padding to 4. Native byte
# order, which is little-endian everywhere this runs.
WL_DISPLAY = 1


class WaylandError(Exception):
    pass


def wl_message(obj, opcode, *args):
    """Encode one request. int arguments are u32 (uint, object, new_id), str are strings."""
    body = b""
    for a in args:
        if isinstance(a, str):
            raw = a.encode() + b"\0"
            body += struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)
        else:
            body += struct.pack("<I", a)
    return struct.pack("<II", obj, (8 + len(body)) << 16 | opcode) + body


def wl_split(buf):
    """Split received bytes into whole messages: ([(obj, opcode, body)], rest)."""
    out = []
    while len(buf) >= 8:
        obj, word = struct.unpack_from("<II", buf)
        size = word >> 16
        if size < 8:
            raise WaylandError(f"bad message size {size}")
        if len(buf) < size:
            break
        out.append((obj, word & 0xFFFF, buf[8:size]))
        buf = buf[size:]
    return out, buf


def wl_args(body, signature):
    """Decode a message body: u = uint, i = int, s = string."""
    out, off = [], 0
    for kind in signature:
        if kind == "s":
            n = struct.unpack_from("<I", body, off)[0]
            out.append(body[off + 4:off + 4 + n - 1].decode("utf-8", "replace") if n else None)
            off += 4 + n + (-n % 4)
        else:
            out.append(struct.unpack_from("<i" if kind == "i" else "<I", body, off)[0])
            off += 4
    return out


def wayland_socket_path(env=os.environ):
    """$WAYLAND_DISPLAY's socket, else the first wayland-N socket in the runtime dir."""
    name = env.get("WAYLAND_DISPLAY")
    if name:
        return name if os.path.isabs(name) else os.path.join(_runtime_dir(env), name)
    try:
        names = sorted(n for n in os.listdir(_runtime_dir(env))
                       if re.fullmatch(r"wayland-\d+", n))
    except OSError:
        names = []
    return os.path.join(_runtime_dir(env), names[0]) if names else None


class WaylandIdle:
    """A Wayland client that asks the compositor to say when the user has been idle
    for `timeout` seconds (ext_idle_notification_v1 "idled") and when they're back
    ("resumed"). It only listens: it creates no idle inhibitor, so it never keeps the
    screen on. The notification follows the compositor's idea of idle, so a playing
    video that holds an idle inhibitor counts as being there.

    Where the compositor has hyprland_lock_notifier_v1 it also hears "locked" and
    "unlocked" (sent at once if the session is already locked)."""

    def __init__(self, timeout, path=None, on_change=None, now=time.time):
        self.timeout = timeout
        self.path = path or wayland_socket_path()
        self.on_change = on_change
        self.now = now
        self.idled_at = None      # wall time of the last "idled", None when active
        self.locked = None        # None: the compositor doesn't say
        self.lock_notification = None
        self.connected = False
        self.error = None
        self._ready = threading.Event()
        self._sock = None
        self._next_id = 2
        self._buf = b""

    def _new_id(self):
        i, self._next_id = self._next_id, self._next_id + 1
        return i

    def _send(self, data):
        self._sock.sendall(data)

    def _read(self):
        data = self._sock.recv(65536)
        if not data:
            raise WaylandError("compositor closed the connection")
        msgs, self._buf = wl_split(self._buf + data)
        for obj, opcode, body in msgs:
            if obj == WL_DISPLAY and opcode == 0:  # wl_display.error
                _, code, message = wl_args(body, "uus")
                raise WaylandError(f"protocol error {code}: {message}")
        return msgs

    def _handshake(self):
        registry, done = self._new_id(), self._new_id()
        self._send(wl_message(WL_DISPLAY, 1, registry) + wl_message(WL_DISPLAY, 0, done))
        globals_ = {}
        while True:
            for obj, opcode, body in self._read():
                if obj == registry and opcode == 0:  # wl_registry.global
                    name, interface, version = wl_args(body, "usu")
                    globals_.setdefault(interface, (name, version))
                elif obj == done and opcode == 0:  # wl_callback.done
                    return registry, globals_

    def _bind(self, registry, globals_, interface):
        name, _ = globals_[interface]
        new = self._new_id()
        self._send(wl_message(registry, 0, name, interface, 1, new))  # version 1
        return new

    def connect(self):
        """Connect and subscribe. Raises WaylandError / OSError when it can't."""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect(self.path)
        registry, globals_ = self._handshake()
        missing = [i for i in ("wl_seat", "ext_idle_notifier_v1") if i not in globals_]
        if missing:
            raise WaylandError("compositor has no " + " or ".join(missing))
        seat = self._bind(registry, globals_, "wl_seat")
        notifier = self._bind(registry, globals_, "ext_idle_notifier_v1")
        self.notification = self._new_id()
        # ext_idle_notifier_v1.get_idle_notification(id, timeout ms, seat)
        self._send(wl_message(notifier, 1, self.notification,
                              max(1, int(self.timeout * 1000)), seat))
        if "hyprland_lock_notifier_v1" in globals_:
            lock_notifier = self._bind(registry, globals_, "hyprland_lock_notifier_v1")
            self.lock_notification = self._new_id()
            # hyprland_lock_notifier_v1.get_lock_notification(id)
            self._send(wl_message(lock_notifier, 1, self.lock_notification))
            self.locked = False
        self._sock.settimeout(None)
        self.connected = True

    def _handle(self, obj, opcode):
        if obj == self.notification:      # 0 idled, 1 resumed
            self.idled_at = self.now() if opcode == 0 else None
        elif obj == self.lock_notification:  # 0 locked, 1 unlocked
            self.locked = opcode == 0
        else:
            return
        if self.on_change:
            self.on_change()

    def serve(self):
        while True:
            for obj, opcode, _ in self._read():
                self._handle(obj, opcode)

    def run(self):
        """Connect, then listen until the connection ends; reconnect after a pause."""
        while True:
            try:
                self.connect()
                self._ready.set()
                self.serve()
            except (OSError, WaylandError, struct.error) as e:
                self.error = str(e) or type(e).__name__
            first = not self._ready.is_set()
            self.connected = False
            self.idled_at = None
            self.locked, self.lock_notification = None, None
            if self._sock:
                self._sock.close()
            self._sock, self._next_id, self._buf = None, 2, b""
            self._ready.set()
            if first:
                return  # no idle protocol here: let the caller use another source
            time.sleep(10)

    def start(self, wait=2.0):
        threading.Thread(target=self.run, daemon=True, name="wayland-idle").start()
        self._ready.wait(wait)
        return self.connected

    def idle_seconds(self):
        idled_at = self.idled_at
        return 0.0 if idled_at is None else self.now() - idled_at + self.timeout


# ---- logind ------------------------------------------------------------------
def _busctl(*args):
    """busctl's answer, one value per property asked for."""
    out = subprocess.run(["busctl", "--json=short", *args], capture_output=True, text=True,
                         timeout=2)
    if out.returncode != 0:
        raise OSError(out.stderr.strip() or "busctl failed")
    return [json.loads(line)["data"] for line in out.stdout.splitlines() if line.strip()]


def logind_session_path():
    """The user's graphical session (the agent may run outside it, as a user service)."""
    try:
        display = _busctl("get-property", "org.freedesktop.login1",
                          "/org/freedesktop/login1/user/self", "org.freedesktop.login1.User",
                          "Display")[0]
        if display and display[1] and display[1] != "/":
            return display[1]
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return None


def logind_idle_seconds(session, now=time.time):
    hint, since = _busctl("get-property", "org.freedesktop.login1", session,
                          "org.freedesktop.login1.Session", "IdleHint", "IdleSinceHint")
    if not hint or not since:
        return 0.0
    return max(0.0, now() - since / 1e6)


def parse_logind_signal(line, session):
    """One line of `gdbus monitor` output -> ("sleep" | "locked", bool), or None.

    /org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)
    /org/freedesktop/login1/session/_2: org.freedesktop.DBus.Properties.PropertiesChanged
        ('org.freedesktop.login1.Session', {'LockedHint': <true>}, @as [])"""
    if ": org.freedesktop.login1.Manager.PrepareForSleep (" in line:
        return "sleep", "(true" in line
    if session and line.startswith(session + ": ") and ".PropertiesChanged (" in line:
        m = re.search(r"'LockedHint': <(true|false)>", line)
        if m:
            return "locked", m.group(1) == "true"
    return None


class LogindWatcher:
    """Follows logind's PrepareForSleep signal and the session's LockedHint by reading
    `gdbus monitor --system` (which needs no privileges), restarting it if it exits."""

    def __init__(self, session, on_change=None, now=time.time,
                 command=("gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1")):
        self.session = session
        self.on_change = on_change
        self.now = now
        self.command = list(command)
        self.locked = False
        self.sleep_since = None   # wall time of PrepareForSleep(true), until (false)
        self.proc = None

    def _read_locked(self):
        try:
            self.locked = bool(_busctl("get-property", "org.freedesktop.login1", self.session,
                                       "org.freedesktop.login1.Session", "LockedHint")[0])
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            pass

    def feed(self, line):
        got = parse_logind_signal(line, self.session)
        if not got:
            return
        kind, value = got
        if kind == "sleep":
            self.sleep_since = (self.sleep_since or self.now()) if value else None
        else:
            self.locked = value
        if self.on_change:
            self.on_change()

    def run(self):
        while True:
            self._read_locked()  # it may have changed while nothing was watching
            try:
                self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True)
                for line in self.proc.stdout:
                    self.feed(line)
                self.proc.wait()
            except OSError as e:
                print(f"[linux] logind watch failed: {e}", flush=True)
            time.sleep(10)

    def start(self):
        atexit.register(self.stop)
        threading.Thread(target=self.run, daemon=True, name="logind").start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---- idle source -------------------------------------------------------------
_IDLE = {"source": None, "at": 0.0}
_settings = {"afk_threshold": 60.0, "on_change": None}


def configure(afk_threshold=None, on_change=None):
    """Called by the agent at start: the idle threshold (the Wayland notification is
    created for it) and a callback for state changes, so the agent can act at once."""
    if afk_threshold is not None:
        _settings["afk_threshold"] = float(afk_threshold)
    _settings["on_change"] = on_change
    _detect()
    _pick_idle_source()
    _start_logind()


_LOGIND = {"started": False, "watcher": None}


def _start_logind():
    if _LOGIND["started"]:
        return _LOGIND["watcher"]
    _LOGIND["started"] = True
    session = shutil.which("gdbus") and shutil.which("busctl") and logind_session_path()
    if session:
        _LOGIND["watcher"] = LogindWatcher(session, on_change=_settings["on_change"])
        _LOGIND["watcher"].start()
    client = _IDLE.get("client")
    sources = [name for name, ok in (("logind LockedHint", session),
                                     ("Hyprland lock-notify", client and client.locked is not None))
               if ok]
    print(f"[linux] lock: {' + '.join(sources) or 'none'}; sleep: "
          f"{'logind PrepareForSleep' if session else 'gaps in wall time only'}", flush=True)
    return _LOGIND["watcher"]


def _pick_idle_source():
    """The first idle source that works. Anything but Wayland is looked at again every
    30 s, so an agent started before the compositor switches to it."""
    if _IDLE["source"] == "wayland" or (
            _IDLE["source"] is not None and time.time() - _IDLE["at"] < 30):
        return _IDLE["source"]
    _IDLE["at"] = time.time()
    source, why = None, []
    path = wayland_socket_path() if hasattr(socket, "AF_UNIX") else None
    if path:
        client = WaylandIdle(_settings["afk_threshold"], path=path,
                             on_change=_settings["on_change"])
        if client.start():
            _IDLE["client"] = client
            source = "wayland"
        else:
            why.append(f"Wayland: {client.error}")
    if source is None:
        session = shutil.which("busctl") and logind_session_path()
        if _detect() == "x11" and shutil.which("xprintidle"):
            source = "xprintidle"
        elif session:
            source = "logind"
            _IDLE["session"] = session
        else:
            source = "none"
    if source != _IDLE["source"]:
        names = {"wayland": f"Wayland ext-idle-notify ({_settings['afk_threshold']:g} s)",
                 "xprintidle": "xprintidle", "logind": "logind IdleHint",
                 "none": "none, nothing will be marked idle"}
        print(f"[linux] idle: {names[source]}" + (f" ({'; '.join(why)})" if why else ""),
              flush=True)
    _IDLE["source"] = source
    return source


# ---- public API ------------------------------------------------------------
def get_active_window():
    mode = _detect()
    if mode == "x11":
        return _x11_active_window()
    if mode == "hyprland":
        events = _HYPR["events"]
        if events and events.connected:
            return events.window
        return _hypr_active_window()
    return None, None


def describe():
    """For the agent's banner: the backends actually in use."""
    mode = _detect()
    events = _HYPR["events"]
    window = {"hyprland": "hyprland (event socket)" if events and events.connected
              else "hyprland (request socket)", "x11": "x11"}.get(mode, "none (retrying)")
    return f"{window}, idle={_pick_idle_source()}"


def is_locked():
    """True while the session is locked; None when nothing here can tell."""
    watcher = _start_logind()
    client = _IDLE.get("client")
    states = [watcher.locked] if watcher else []
    if client and client.locked is not None:
        states.append(client.locked)
    return any(states) if states else None


def sleep_started():
    """Wall time logind announced the machine is going to sleep, until it's back."""
    watcher = _start_logind()
    return watcher.sleep_since if watcher else None


def get_idle_seconds():
    source = _pick_idle_source()
    if source == "wayland":
        return _IDLE["client"].idle_seconds()
    if source == "xprintidle":
        return _x11_idle()
    if source == "logind":
        try:
            return logind_idle_seconds(_IDLE["session"])
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return 0.0
    return 0.0
