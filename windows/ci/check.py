"""CI only: the checks the windows job makes, each printing what it saw.

    python check.py events LOG --device NAME --type boot [--timeout 60] [--after N]
        waits until the hub took (200, none rejected) a batch from NAME holding an event of
        that type, through the prefix; --after N ignores the log's first N lines.
    python check.py lines LOG                         prints how many lines LOG has
    python check.py bridge PORT [--header NAME:VALUE] posts a page as the extension would
    python check.py processes PID [PID ...] [--uac-off]
        not elevated (unless UAC is off, when nothing can be), no console, GUI exe
    python check.py exe PATH                          windowed exe with its CA bundle
    python check.py toml FILE KEY EXPECTED_FILE       KEY in FILE is EXPECTED_FILE's text
"""
import argparse
import json
import os
import sys
import time

GUI, CONSOLE = 2, 3


def subsystem(path):
    with open(path, "rb") as f:
        data = f.read(4096)
    pe = int.from_bytes(data[0x3C:0x40], "little")
    assert data[pe:pe + 4] == b"PE\0\0", f"{path} is not a PE file"
    return int.from_bytes(data[pe + 24 + 68:pe + 24 + 70], "little")


def events(a):
    deadline = time.time() + a.timeout
    seen = []
    while time.time() < deadline:
        if os.path.exists(a.log):
            with open(a.log, encoding="utf-8") as f:
                seen = [json.loads(line) for line in f.readlines()[a.after:]]
            for e in seen:
                if (e.get("device") == a.device and e.get("status") == 200
                        and a.type in e.get("types", []) and not e.get("rejected")):
                    print(f"hub took {e['types']} from {a.device!r} at {e['path']}")
                    return 0
        time.sleep(1)
    print(f"no {a.type!r} event from {a.device!r} reached the hub in {a.timeout} s; requests:")
    for e in seen:
        print("  ", json.dumps(e, ensure_ascii=False))
    return 1


def lines(a):
    n = 0
    if os.path.exists(a.log):
        with open(a.log, encoding="utf-8") as f:
            n = len(f.readlines())
    print(n)
    return 0


def bridge(a):
    import urllib.request
    now = time.time()
    body = json.dumps({"id": os.urandom(16).hex(), "url": "https://example.com/ci?q=1",
                       "title": "CI page", "domain": "example.com", "ts_start": now - 5,
                       "ts_end": now, "duration": 5.0}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{a.port}/browser", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "chrome-extension://abcdefghijklmnopabcdefghijklmnop")
    for h in a.header:
        name, _, value = h.partition(":")
        req.add_header(name.strip(), value.strip())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"bridge on 127.0.0.1:{a.port} answered {r.status}")
            return 0 if r.status == 200 else 1
    except OSError as e:
        print(f"bridge on 127.0.0.1:{a.port}: {e}")
        return 1


def processes(a):
    import ctypes
    from ctypes import wintypes as w
    import psutil

    k32, adv = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("advapi32", use_last_error=True)
    k32.OpenProcess.restype = w.HANDLE
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.POINTER(w.DWORD), w.DWORD,
                                        ctypes.POINTER(w.DWORD)]
    bad = 0
    for pid in a.pids:
        p = psutil.Process(pid)
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        tok, elevated, n = w.HANDLE(), w.DWORD(), w.DWORD()
        adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok))  # TOKEN_QUERY
        adv.GetTokenInformation(tok, 20, ctypes.byref(elevated), 4, ctypes.byref(n))  # TokenElevation
        consoles = [c.pid for c in p.children() if c.name().lower() == "conhost.exe"]
        sub = subsystem(p.exe())
        print(f"pid {pid}: {p.exe()} elevated={bool(elevated.value)} "
              f"subsystem={'GUI' if sub == GUI else sub} console hosts={consoles}")
        bad += (bool(elevated.value) and not a.uac_off) or sub != GUI or bool(consoles)
    return 1 if bad else 0


def exe(a):
    sub = subsystem(a.path)
    bundle = os.path.join(os.path.dirname(a.path), "_internal", "certifi", "cacert.pem")
    print(f"{a.path}: subsystem={'GUI (no console)' if sub == GUI else sub}; "
          f"CA bundle {'present' if os.path.exists(bundle) else 'MISSING'}")
    return 0 if sub == GUI and os.path.exists(bundle) else 1


def toml(a):
    import tomllib
    with open(a.file, "rb") as f:
        got = tomllib.load(f)[a.key]
    with open(a.expected, encoding="utf-8") as f:
        want = f.read()
    print(f"{a.key}: wrote {want!r}, read back {got!r}")
    return 0 if got == want else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("events")
    p.add_argument("log")
    p.add_argument("--device", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--after", type=int, default=0)
    p = sub.add_parser("lines")
    p.add_argument("log")
    p = sub.add_parser("bridge")
    p.add_argument("port", type=int)
    p.add_argument("--header", action="append", default=[])
    p = sub.add_parser("processes")
    p.add_argument("pids", type=int, nargs="+")
    p.add_argument("--uac-off", action="store_true")
    p = sub.add_parser("exe")
    p.add_argument("path")
    p = sub.add_parser("toml")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("expected")
    a = ap.parse_args()
    return globals()[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
