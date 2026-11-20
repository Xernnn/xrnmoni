"""Run the xrnmoni agent on Windows without a console. The logon task and
xrnmoni-agent.exe start this; every argument is passed on to agent.py:

    pythonw.exe launcher.py --config "%APPDATA%\\xrnmoni\\config.toml"

- pythonw (and the windowed exe) has no stdout or stderr: print() there goes nowhere and
  anything that writes to sys.stderr directly fails. Both go to
  %LOCALAPPDATA%\\xrnmoni\\logs\\agent.log instead (1 MB, five old files kept), and so do
  logging, uncaught exceptions and crashes in threads.
- agent.pid in %LOCALAPPDATA%\\xrnmoni holds this process's id while it runs, so
  windows\\stop.ps1 stops this process and nothing else.
- One agent per logon session: a second start logs that and exits.
- When Windows ends the session, or stop.ps1 asks it to close, the agent is stopped through
  its own signal handling (platform_win.start_session_watch), so it writes what it has.

XRNMONI_HOME overrides %LOCALAPPDATA%\\xrnmoni (tests use it).
"""
import faulthandler
import io
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(os.path.dirname(HERE), "agent")
MUTEX = "xrnmoni-agent"
log = logging.getLogger("xrnmoni.launcher")
_keep = []


def home_dir():
    if os.environ.get("XRNMONI_HOME"):
        return os.environ["XRNMONI_HOME"]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "xrnmoni")


class _LogStream(io.TextIOBase):
    """sys.stdout / sys.stderr as a logger: one record per line. Lines are gathered per
    thread so two threads printing at once don't mix, and a write made while logging (a
    handler reporting its own error to sys.stderr) is dropped instead of looping."""

    def __init__(self, logger, level, tee=None):
        self.logger, self.level, self.tee = logger, level, tee
        self._local = threading.local()

    encoding = "utf-8"
    errors = "replace"

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, s):
        if self.tee is not None:
            try:
                self.tee.write(s)
            except Exception:
                pass
        local = self._local
        if getattr(local, "busy", False):
            return len(s)
        local.busy = True
        try:
            buf = getattr(local, "buf", "") + s
            *lines, local.buf = buf.split("\n")
            for line in lines:
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        finally:
            local.busy = False
        return len(s)

    def flush(self):
        if self.tee is not None:
            try:
                self.tee.flush()
            except Exception:
                pass


class _Rotating(RotatingFileHandler):
    def doRollover(self):
        # Renaming fails while something holds the log open without delete sharing (an
        # editor, `Get-Content -Wait`). Keep writing to it then; the next record tries again.
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def setup_logging(log_dir, name="agent", max_bytes=1_000_000, backups=5):
    os.makedirs(log_dir, exist_ok=True)
    handler = _Rotating(os.path.join(log_dir, f"{name}.log"), maxBytes=max_bytes,
                        backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.raiseExceptions = False   # a full disk must not take the agent down
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    stdio = logging.getLogger("xrnmoni.stdio")   # straight to the file, not back to root
    stdio.propagate = False
    stdio.addHandler(handler)
    sys.stdout = _LogStream(stdio, logging.INFO, tee=sys.stdout)
    sys.stderr = _LogStream(stdio, logging.WARNING, tee=sys.stderr)

    def thread_crash(args):
        if args.exc_type is not SystemExit:
            log.error("thread %s crashed", args.thread.name if args.thread else "?",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_crash
    # A hard crash (in a ctypes callback, say) leaves a trace instead of nothing.
    crash = open(os.path.join(log_dir, f"{name}-crash.log"), "a", encoding="utf-8")
    _keep.append(crash)
    faulthandler.enable(file=crash)
    return handler


def _write_pid(path):
    with open(path, "w", encoding="ascii") as f:
        f.write(f"{os.getpid()}\n")


def _remove_pid(path):
    try:
        with open(path, encoding="ascii") as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(path)
    except OSError:
        pass


def _agent_main():
    import agent
    return agent.main()


def run(argv, home=None, agent_main=_agent_main):
    home = home or home_dir()
    setup_logging(os.path.join(home, "logs"))
    log.info("starting (pid %d): %s", os.getpid(), " ".join(argv))
    if not getattr(sys, "frozen", False) and AGENT_DIR not in sys.path:
        sys.path.insert(0, AGENT_DIR)   # the exe has agent.py built in
    if sys.platform == "win32":
        import platform_win
        if not platform_win.claim_single_instance(MUTEX):
            log.info("another xrnmoni agent is already running in this session; exiting")
            return 0
        if not platform_win.start_session_watch():
            log.warning("no hidden window: logoff and stop.ps1 can't stop the agent cleanly")
    pid_file = os.path.join(home, "agent.pid")
    _write_pid(pid_file)
    sys.argv = ["agent.py", *argv]
    try:
        agent_main()
        log.info("agent exited")
        return 0
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    except SystemExit as e:
        log.info("agent exited with %s", e.code)
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        log.exception("agent crashed")
        return 1
    finally:
        _remove_pid(pid_file)
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
