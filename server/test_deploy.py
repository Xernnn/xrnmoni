"""deploy.sh and put-downloads.sh against a made-up server: ssh, sv, curl and tailscale are stand-ins
that run here, and ssh runs the "server's" side in a home of its own, as a real server has.

    python3 -m unittest server/test_deploy.py

The hub's own tests, which deploy.sh runs first, are left to test_xrnmoni.py: here they pass.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON3 = shutil.which("python3")

FAKES = {
    # ssh HOST SCRIPT: the script runs here, in the server's made-up home.
    "ssh": '#!/bin/sh\nshift\nHOME=$SERVER_HOME exec sh -c "$*"\n',
    "sv": '#!/bin/sh\necho "sv $*" >> "$HOME/calls"\nexit 0\n',
    "curl": "#!/bin/sh\nprintf ok\n",
    "tailscale": '#!/bin/sh\necho "tailscale $*" >> "$HOME/calls"\n',
    "python3": f"""#!/bin/sh
if [ "$*" = "-m unittest server/test_xrnmoni.py" ]; then
    exit 0
fi
exec {PYTHON3} "$@"
""",
}

# What server/deploy.local holds for the home server (git keeps the real one out).
HOME_SERVER = """XRNMONI_HOST=homeserver
XRNMONI_DIR=xrnmoni
XRNMONI_PORT=5245
XRNMONI_PREFIX=/xrnmoni
XRNMONI_USERS={users}
XRNMONI_SVGUARD={guard}
XRNMONI_PUBLISH='tailscale funnel --bg --set-path /xrnmoni http://127.0.0.1:5245 >/dev/null && tailscale funnel status'
"""


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path):
    with open(path) as f:
        return f.read()


@unittest.skipUnless(sys.platform != "win32" and shutil.which("sh") and shutil.which("tar") and PYTHON3,
                     "needs sh, tar and python3, not Windows")
class Deploy(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="xrnmoni-deploy-")
        self.addCleanup(shutil.rmtree, self.root)
        self.laptop = os.path.join(self.root, "laptop")
        self.server = os.path.join(self.root, "server-home")
        self.prefix = os.path.join(self.root, "usr")
        self.bin = os.path.join(self.root, "bin")
        self.repo = os.path.join(self.root, "repo")
        for d in (self.laptop, self.server, os.path.join(self.prefix, "var", "service"), self.bin,
                  os.path.join(self.repo, "server")):
            os.makedirs(d)
        for name, body in FAKES.items():
            write(os.path.join(self.bin, name), body, stat.S_IRWXU)
        # A copy of server/ to deploy from, with its own deploy.local.
        for name in ("deploy.sh", "put-downloads.sh"):
            shutil.copy(os.path.join(HERE, name), os.path.join(self.repo, "server", name))
        write(os.path.join(self.repo, "server", "xrnmoni.py"), "# the hub\n")
        write(os.path.join(self.repo, "server", "web", "index.html"), "<!doctype html>\n")
        write(os.path.join(self.server, ".local", "bin", "sv-guard"), "#!/bin/sh\nexit 0\n", 0o755)

    def run_script(self, name, *args, local=None):
        if local is not None:
            write(os.path.join(self.repo, "server", "deploy.local"), local)
        open(os.path.join(self.server, "calls"), "w").close()
        env = {**os.environ, "HOME": self.laptop, "SERVER_HOME": self.server, "PREFIX": self.prefix,
               "PATH": f"{self.bin}:{os.environ['PATH']}", "PYTHONDONTWRITEBYTECODE": "1"}
        for k in [k for k in env if k.startswith("XRNMONI_")]:
            del env[k]
        return subprocess.run(["sh", os.path.join(self.repo, "server", name), *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def run_file(self):
        return read(os.path.join(self.prefix, "var", "service", "xrnmoni", "run"))

    def test_the_home_server_settings_make_its_service(self):
        local = HOME_SERVER.format(users="'~/.config/xrn-sync/users'", guard="'~/.local/bin/sv-guard'")
        done = self.run_script("deploy.sh", local=local)
        self.assertEqual(0, done.returncode, done.stderr)
        hub = os.path.join(self.server, "xrnmoni")
        run = self.run_file()
        self.assertIn(f'"{self.server}/.local/bin/sv-guard" 5245 "{hub}/xrnmoni.py" || exit 1\n', run)
        self.assertIn(f'"{hub}/xrnmoni.py" --port 5245 --data "{hub}/data" --prefix "/xrnmoni" '
                      f'--users "{self.server}/.config/xrn-sync/users" 2>&1', run)
        self.assertLess(run.index("sv-guard"), run.index("exec "))
        self.assertEqual("# the hub\n", read(os.path.join(hub, "xrnmoni.py")))
        for d in ("data", os.path.join("data", "downloads")):
            self.assertEqual(0o700, stat.S_IMODE(os.stat(os.path.join(hub, d)).st_mode), d)
        calls = read(os.path.join(self.server, "calls"))
        self.assertIn("tailscale funnel --bg --set-path /xrnmoni http://127.0.0.1:5245", calls)
        self.assertNotIn("serve", calls)
        self.assertNotIn(self.laptop, run)

    def test_an_unquoted_tilde_in_deploy_local_is_still_the_servers_home(self):
        # The shell reading deploy.local turns ~/ into this machine's home: the run file named the
        # laptop's users file, which isn't on the server, and no one could sign in.
        local = HOME_SERVER.format(users="~/.config/xrn-sync/users", guard="~/.local/bin/sv-guard")
        done = self.run_script("deploy.sh", local=local)
        self.assertEqual(0, done.returncode, done.stderr)
        run = self.run_file()
        self.assertIn(f'--users "{self.server}/.config/xrn-sync/users"', run)
        self.assertIn(f'"{self.server}/.local/bin/sv-guard" 5245', run)
        self.assertNotIn(self.laptop, run)

    def test_a_missing_guard_stops_it_before_the_service_is_written(self):
        os.remove(os.path.join(self.server, ".local", "bin", "sv-guard"))
        local = HOME_SERVER.format(users="'~/.config/xrn-sync/users'", guard="'~/.local/bin/sv-guard'")
        done = self.run_script("deploy.sh", local=local)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("sv-guard", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "var", "service", "xrnmoni")))

    def test_downloads_go_whole_into_the_folder_the_get_page_lists(self):
        local = HOME_SERVER.format(users="'~/.config/xrn-sync/users'", guard="'~/.local/bin/sv-guard'")
        files = os.path.join(self.root, "dist")
        write(os.path.join(files, "xrnmoni-1.1.0.apk"), "apk bytes")
        write(os.path.join(files, "xrnmoni-windows-1.1.0.zip"), "zip bytes")
        done = self.run_script("put-downloads.sh", os.path.join(files, "xrnmoni-1.1.0.apk"),
                               os.path.join(files, "xrnmoni-windows-1.1.0.zip"), local=local)
        self.assertEqual(0, done.returncode, done.stderr)
        folder = os.path.join(self.server, "xrnmoni", "data", "downloads")
        self.assertEqual(["xrnmoni-1.1.0.apk", "xrnmoni-windows-1.1.0.zip"], sorted(os.listdir(folder)))
        self.assertEqual("apk bytes", read(os.path.join(folder, "xrnmoni-1.1.0.apk")))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(os.path.join(folder, "xrnmoni-1.1.0.apk")).st_mode))

    def test_a_name_the_hub_wouldnt_serve_sends_nothing(self):
        local = HOME_SERVER.format(users="'~/.config/xrn-sync/users'", guard="'~/.local/bin/sv-guard'")
        good = os.path.join(self.root, "dist", "xrnmoni-1.1.0.apk")
        write(good, "apk")
        for bad in ("notes.txt", "my app.apk", ".hidden.apk"):
            path = os.path.join(self.root, "dist", bad)
            write(path, "x")
            done = self.run_script("put-downloads.sh", good, path, local=local)
            self.assertEqual(2, done.returncode, bad)
        self.assertFalse(os.path.exists(os.path.join(self.server, "xrnmoni", "data", "downloads")))


if __name__ == "__main__":
    unittest.main()
