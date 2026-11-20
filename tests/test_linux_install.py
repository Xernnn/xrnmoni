"""linux/install.sh: venv, config and systemd user unit, against stubbed pip and systemctl."""
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest

from agent_helpers import ROOT

INSTALL = os.path.join(ROOT, "linux", "install.sh")
UNIT = os.path.join(ROOT, "linux", "xrnmoni-agent.service")


def unit_values(text):
    values = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


class UnitTest(unittest.TestCase):
    def test_the_unit_runs_with_the_graphical_session(self):
        with open(UNIT, encoding="utf-8") as f:
            v = unit_values(f.read())
        self.assertEqual(v["WantedBy"], ["graphical-session.target"])
        self.assertEqual(v["PartOf"], ["graphical-session.target"])
        self.assertEqual(v["After"], ["graphical-session.target"])
        self.assertEqual(v["Restart"], ["on-failure"])


@unittest.skipIf(sys.platform.startswith("win"), "bash and systemd")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(suffix=" a b&c%")   # spaces and odd characters
        self.bin = os.path.join(self.home, "bin")
        self.venv = os.path.join(self.home, "venv")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.venv, "bin"))
        os.symlink(sys.executable, os.path.join(self.venv, "bin", "python"))
        self.log = os.path.join(self.home, "calls.log")
        for name, path in (("systemctl", self.bin), ("pip", os.path.join(self.venv, "bin"))):
            stub = os.path.join(path, name)
            with open(stub, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n')
            os.chmod(stub, stat.S_IRWXU)
        self.config_home = os.path.join(self.home, ".config")

    def install(self):
        env = dict(os.environ, HOME=self.home, XDG_CONFIG_HOME=self.config_home,
                   XRNMONI_VENV=self.venv, PATH=self.bin + os.pathsep + os.environ["PATH"])
        env.pop("XRNMONI_CONFIG", None)
        r = subprocess.run(["bash", INSTALL], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_a_first_install_and_a_second(self):
        out = self.install()
        config = os.path.join(self.config_home, "xrnmoni", "agent.toml")
        self.assertEqual(stat.S_IMODE(os.stat(config).st_mode), 0o600)
        with open(config, "rb") as f:
            cfg = tomllib.load(f)
        self.assertRegex(cfg["bridge_secret"], r"^[A-Za-z0-9_-]{32}$")
        self.assertIn(cfg["bridge_secret"], out)

        unit = os.path.join(self.config_home, "systemd", "user", "xrnmoni-agent.service")
        with open(unit, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("@", re.sub(r"(?m)^#.*$", "", text))  # every placeholder filled
        exec_start = unit_values(text)["ExecStart"][0]
        args = [a.replace("%%", "%") for a in shlex.split(exec_start)]
        self.assertEqual(args, [os.path.join(self.venv, "bin", "python"),
                                os.path.join(ROOT, "agent", "agent.py"), "--config", config])
        self.assertEqual(self.calls(), [
            f"pip install --quiet -r {ROOT}/agent/requirements.txt",
            "systemctl --user daemon-reload",
            "systemctl --user enable xrnmoni-agent.service"])  # not started: config unedited

        os.remove(self.log)
        self.install()
        with open(config, "rb") as f:
            self.assertEqual(tomllib.load(f)["bridge_secret"], cfg["bridge_secret"])  # kept
        self.assertEqual(self.calls()[-1], "systemctl --user restart xrnmoni-agent.service")


if __name__ == "__main__":
    unittest.main()
