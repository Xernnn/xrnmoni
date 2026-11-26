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

    def install(self, *args):
        env = dict(os.environ, HOME=self.home, XDG_CONFIG_HOME=self.config_home,
                   XRNMONI_VENV=self.venv, PATH=self.bin + os.pathsep + os.environ["PATH"])
        env.pop("XRNMONI_CONFIG", None)
        r = subprocess.run(["bash", INSTALL, *args], env=env, capture_output=True, text=True)
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

    def test_the_hub_token_and_name_given_start_it_at_once(self):
        token = "xrnmoni_0a1b2c3d_" + "Q" * 43
        out = self.install("--hub", "https://hub.example.net/xrnmoni", "--token", token,
                           "--device", 'Sam\'s "laptop" \u00e9')
        config = os.path.join(self.config_home, "xrnmoni", "agent.toml")
        with open(config, "rb") as f:
            cfg = tomllib.load(f)
        self.assertEqual((cfg["server_url"], cfg["token"], cfg["device"]),
                         ("https://hub.example.net/xrnmoni", token, 'Sam\'s "laptop" \u00e9'))
        self.assertEqual(cfg["poll_interval"], 2.0)  # the rest of the example is kept
        self.assertEqual(self.calls()[-1], "systemctl --user restart xrnmoni-agent.service")
        self.assertIn(cfg["bridge_secret"], out)
        self.assertNotIn("Set server_url", out)
        self.assertEqual(stat.S_IMODE(os.stat(config).st_mode), 0o600)

        # A new token later replaces the old one and keeps everything else.
        os.remove(self.log)
        self.install("--token", "xrnmoni_99999999_" + "R" * 43)
        with open(config, "rb") as f:
            again = tomllib.load(f)
        self.assertEqual(again["token"], "xrnmoni_99999999_" + "R" * 43)
        self.assertEqual((again["server_url"], again["bridge_secret"]), (cfg["server_url"], cfg["bridge_secret"]))

    def test_an_unknown_option_changes_nothing(self):
        env = dict(os.environ, HOME=self.home, XDG_CONFIG_HOME=self.config_home,
                   XRNMONI_VENV=self.venv, PATH=self.bin + os.pathsep + os.environ["PATH"])
        r = subprocess.run(["bash", INSTALL, "--hubb", "x"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.config_home, "xrnmoni")))


if __name__ == "__main__":
    unittest.main()
