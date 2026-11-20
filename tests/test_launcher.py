"""windows/launcher.py: what the logon task and the exe run, with no console.

Each test runs the launcher in a child process with sys.stdout and sys.stderr set to None,
as pythonw.exe and a windowed PyInstaller exe start it, and a made-up agent in place of
agent.py.

    python -m unittest tests.test_launcher
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.path.join(ROOT, "windows")

DRIVER = """
import os, sys, threading, time
sys.stdout = None
sys.stderr = None
sys.path.insert(0, {windows!r})
import launcher

HOME = {home!r}

def fake_agent():
{body}

sys.exit(launcher.run({argv!r}, home=HOME, agent_main=fake_agent))
"""


class LauncherTest(unittest.TestCase):
    def run_launcher(self, body, argv=("--config", "c.toml")):
        home = tempfile.mkdtemp()
        script = DRIVER.format(windows=WINDOWS, home=home, argv=list(argv),
                               body=textwrap.indent(textwrap.dedent(body), "    "))
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           timeout=30)
        log_path = os.path.join(home, "logs", "agent.log")
        log = ""
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                log = f.read()
        return r, home, log

    def test_output_without_a_console_goes_to_the_log(self):
        r, home, log = self.run_launcher("""
            print("[xrnmoni] device=pc os=windows", flush=True)
            sys.stdout.write("half a ")
            sys.stdout.write("line\\n")
            sys.stderr.write("[sender] server said 500\\n")
            import logging
            logging.getLogger("agent").warning("from logging")
        """)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout + r.stderr, "")   # nothing tried to reach a console
        self.assertIn("starting (pid", log)
        self.assertIn("--config c.toml", log)
        self.assertIn("INFO xrnmoni.stdio: [xrnmoni] device=pc os=windows", log)
        self.assertIn("INFO xrnmoni.stdio: half a line", log)
        self.assertIn("WARNING xrnmoni.stdio: [sender] server said 500", log)
        self.assertIn("WARNING agent: from logging", log)

    def test_agent_sees_its_arguments(self):
        r, home, log = self.run_launcher("""
            print("argv", sys.argv)
        """, argv=["--config", "My Documents/config.toml"])
        self.assertIn("argv ['agent.py', '--config', 'My Documents/config.toml']", log)

    def test_pid_file_while_running_and_gone_after(self):
        r, home, log = self.run_launcher("""
            with open(os.path.join(HOME, "agent.pid")) as f:
                print("pid file says", f.read().strip(), "and I am", os.getpid())
        """)
        line = [l for l in log.splitlines() if "pid file says" in l][0]
        said, me = line.split("pid file says ")[1].split(" and I am ")
        self.assertEqual(said, me)
        self.assertFalse(os.path.exists(os.path.join(home, "agent.pid")))

    def test_a_crash_is_logged_and_exits_non_zero(self):
        r, home, log = self.run_launcher("""
            raise RuntimeError("boom")
        """)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR xrnmoni.launcher: agent crashed", log)
        self.assertIn("RuntimeError: boom", log)

    def test_a_crash_in_a_thread_is_logged(self):
        r, home, log = self.run_launcher("""
            def die():
                raise ValueError("sender died")
            t = threading.Thread(target=die, name="Sender")
            t.start(); t.join()
        """)
        self.assertEqual(r.returncode, 0)
        self.assertIn("thread Sender crashed", log)
        self.assertIn("ValueError: sender died", log)

    def test_stop_by_keyboard_interrupt_or_exit_code(self):
        r, home, log = self.run_launcher("""
            raise KeyboardInterrupt
        """)
        self.assertEqual(r.returncode, 0)
        self.assertIn("xrnmoni.launcher: stopped", log)
        r, home, log = self.run_launcher("""
            raise SystemExit(2)
        """)
        self.assertEqual(r.returncode, 2)

    def test_log_rotates(self):
        r, home, log = self.run_launcher("""
            import logging
            for h in logging.getLogger().handlers:
                h.maxBytes = 2000
            for i in range(200):
                print("line", i, "x" * 40)
        """)
        logs = sorted(os.listdir(os.path.join(home, "logs")))
        self.assertIn("agent.log.1", logs)
        self.assertNotIn("agent.log.6", logs)
        for name in logs:
            if name.startswith("agent.log"):
                self.assertLess(os.path.getsize(os.path.join(home, "logs", name)), 4000)


if __name__ == "__main__":
    unittest.main()
