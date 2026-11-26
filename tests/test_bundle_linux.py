"""scripts/bundle-linux.py: the Linux agent as one .tar.gz that installs with no checkout."""
import importlib.util
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest

from agent_helpers import ROOT

spec = importlib.util.spec_from_file_location("bundle_linux", os.path.join(ROOT, "scripts", "bundle-linux.py"))
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = self.tmp.name

    def test_it_holds_what_the_installer_needs_under_one_folder(self):
        path = bundle.build(self.out, "1.2.3")
        self.assertEqual(os.path.join(self.out, "xrnmoni-linux-1.2.3.tar.gz"), path)
        with tarfile.open(path) as tar:
            members = {m.name: m for m in tar.getmembers()}
        top = "xrnmoni-linux-1.2.3/"
        self.assertTrue(all(n.startswith(top) for n in members))
        for need in (*bundle.FILES, "extension/manifest.json", "extension/background.js", "extension/icons/icon128.png"):
            self.assertIn(top + need, members, need)
        self.assertEqual(0o755, members[top + "linux/install.sh"].mode)
        self.assertEqual(0o644, members[top + "agent/agent.py"].mode)
        for name in members:
            self.assertNotRegex(name, r"__pycache__|\.pyc$|/\.|config\.toml$|platform_win|windows/")
        self.assertEqual({0}, {m.uid for m in members.values()})

    def test_the_same_files_make_the_same_bytes(self):
        first = bundle.build(os.path.join(self.out, "a"), "1.0.0")
        second = bundle.build(os.path.join(self.out, "b"), "1.0.0")
        with open(first, "rb") as f, open(second, "rb") as g:
            self.assertEqual(f.read(), g.read())

    def test_a_version_that_isnt_one_is_refused(self):
        for bad in ("../x", "1.0 beta", "v", "1" * 50):
            with self.assertRaises(SystemExit, msg=bad):
                bundle.build(self.out, bad)

    @unittest.skipIf(sys.platform.startswith("win"), "bash and systemd")
    def test_unpacked_it_installs_and_the_service_runs_from_it(self):
        path = bundle.build(self.out, "1.2.3")
        with tarfile.open(path) as tar:
            tar.extractall(self.out, filter="data")
        unpacked = os.path.join(self.out, "xrnmoni-linux-1.2.3")
        home = os.path.join(self.out, "home")
        stubs = os.path.join(home, "bin")
        venv = os.path.join(home, "venv")
        os.makedirs(stubs)
        os.makedirs(os.path.join(venv, "bin"))
        os.symlink(sys.executable, os.path.join(venv, "bin", "python"))
        for name, folder in (("systemctl", stubs), ("pip", os.path.join(venv, "bin"))):
            with open(os.path.join(folder, name), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(folder, name), stat.S_IRWXU)
        config_home = os.path.join(home, ".config")
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=config_home, XRNMONI_VENV=venv,
                   PATH=stubs + os.pathsep + os.environ["PATH"])
        env.pop("XRNMONI_CONFIG", None)
        token = "xrnmoni_0a1b2c3d_" + "Q" * 43
        r = subprocess.run(["bash", os.path.join(unpacked, "linux", "install.sh"), "--hub", "https://hub.example.net/xrnmoni",
                            "--token", token, "--device", "kid-laptop"], env=env, capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)
        with open(os.path.join(config_home, "xrnmoni", "agent.toml"), "rb") as f:
            self.assertEqual("kid-laptop", tomllib.load(f)["device"])
        with open(os.path.join(config_home, "systemd", "user", "xrnmoni-agent.service"), encoding="utf-8") as f:
            exec_start = next(line for line in f if line.startswith("ExecStart="))
        args = shlex.split(exec_start.partition("=")[2])
        self.assertEqual(os.path.join(unpacked, "agent", "agent.py"), args[1])


if __name__ == "__main__":
    unittest.main()
