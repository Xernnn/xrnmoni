"""Checks on the Windows install scripts that don't need Windows.

What they do on a real Windows is checked by the `windows` job in .github/workflows/ci.yml
(it installs, starts at logon, stops and uninstalls for real). Here: mistakes that read fine
but break, or that the scripts must never do again. If PowerShell (pwsh) is on PATH, every
script is also parsed.

    python -m unittest tests.test_windows_scripts
"""
import base64
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.path.join(ROOT, "windows")
SCOPES = {"env", "script", "global", "local", "private", "using", "function", "variable", "alias"}


def scripts(*patterns):
    found = []
    for p in patterns:
        found += glob.glob(os.path.join(WINDOWS, p))
    return sorted(found)


def code_lines(path):
    """(number, line) for each line that isn't a comment (good enough for these scripts)."""
    with open(path, encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if s and not s.startswith(("#", "REM ", "rem ", "::")):
                yield n, line


def chromium_id():
    """Chrome's id for an extension with a "key": SHA-256 of the key, first 32 hex digits as a-p."""
    with open(os.path.join(ROOT, "extension", "manifest.json"), encoding="utf-8") as f:
        key = json.load(f)["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def read(name):
    with open(os.path.join(WINDOWS, name), encoding="utf-8-sig") as f:
        return f.read()


def read_readme_windows():
    with open(os.path.join(WINDOWS, "README-WINDOWS.md"), encoding="utf-8") as f:
        return f.read()


class PowerShellPitfallsTest(unittest.TestCase):
    def test_no_variable_followed_by_a_colon_in_strings(self):
        # "http://$ip:8000" reads $ip:8000 as a variable in a scope called "ip" (empty), and
        # "in $dir: ..." doesn't parse at all. ${ip}:8000 is what was meant.
        bad = []
        for path in scripts("*.ps1"):
            for n, line in code_lines(path):
                for m in re.finditer(r"\$(?!\{)(\w+):(?!:)", line):
                    if m.group(1).lower() not in SCOPES:
                        bad.append(f"{os.path.basename(path)}:{n}: {line.strip()}")
        self.assertEqual(bad, [])

    def test_text_files_for_python_are_written_without_a_byte_order_mark(self):
        # Windows PowerShell's Set-Content/Out-File write UTF-8 with a BOM (or the ANSI code
        # page), and Python's TOML reader refuses a BOM.
        for path in scripts("*.ps1"):
            for n, line in code_lines(path):
                if re.search(r"config", line, re.I):
                    self.assertNotRegex(line, r"\b(Set-Content|Out-File|Add-Content)\b",
                                        f"{os.path.basename(path)}:{n}")
        self.assertIn("UTF8Encoding($false)", read("common.ps1"))
        self.assertIn("Write-XrnConfig $values", read("install.ps1"))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed")
    def test_every_script_parses(self):
        for path in scripts("*.ps1"):
            with self.subTest(script=os.path.basename(path)):
                cmd = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                       f"'{path}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.ToString() }}")
                r = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True,
                                   text=True, timeout=60)
                self.assertEqual(r.stdout.strip(), "")


class PerUserInstallTest(unittest.TestCase):
    """install.ps1 runs without an administrator and opens nothing to the network."""

    def test_needs_no_administrator(self):
        for path in scripts("*.ps1", "*.bat"):
            text = "".join(line for _, line in code_lines(path))
            with self.subTest(script=os.path.basename(path)):
                self.assertNotRegex(text, r"(?i)/rl\s+highest|RunLevel\s+Highest|RunAsAdministrator")
                self.assertNotRegex(text, r"(?i)New-NetFirewallRule|netsh\s+advfirewall")
                self.assertNotRegex(text, r"(?i)HKLM:|\$env:ProgramData|\$env:windir")

    def test_private_windows_are_turned_off_by_machine_policy_in_every_chromium_browser(self):
        # A parent closes private windows on a child's PC (decided 2026-11-27): a private window is
        # where xrnmoni records only "Private" and its time. Machine policy, which a standard
        # account can't change, so this one script needs an administrator and lives apart from
        # the per-user ones the test above holds to that rule.
        with open(os.path.join(WINDOWS, "admin", "private-windows.ps1"), encoding="utf-8-sig") as f:
            text = f.read()
        for key, name in ((r"HKLM:\SOFTWARE\Policies\Google\Chrome", "IncognitoModeAvailability"),
                          (r"HKLM:\SOFTWARE\Policies\Chromium", "IncognitoModeAvailability"),
                          (r"HKLM:\SOFTWARE\Policies\Microsoft\Edge", "InPrivateModeAvailability"),
                          (r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "IncognitoModeAvailability"),
                          (r"HKLM:\SOFTWARE\Policies\BraveSoftware\Brave", "TorDisabled")):
            with self.subTest(name=name, key=key):
                self.assertRegex(text, re.escape(f"Key = '{key}';") + r"\s+Name = '" + name + r"';\s+Off = 1 }")
        self.assertIn("WindowsBuiltInRole]::Administrator", text)
        self.assertIn("-PropertyType DWord", text)
        self.assertNotRegex(text, r"(?i)Remove-Item\s+-LiteralPath \$p\.Key")  # never the whole key
        self.assertIn("Private windows are off", text)
        self.assertIn("private-windows.ps1", read_readme_windows())

    def test_binds_nothing_to_every_address(self):
        for path in scripts("*.ps1", "*.bat", "*.py"):
            with self.subTest(script=os.path.basename(path)):
                text = "".join(line for _, line in code_lines(path))
                self.assertNotIn("0.0.0.0", text)
        self.assertRegex(read("install.ps1"), r"\[string\]\$HubBind = '127\.0\.0\.1'")

    def test_files_go_to_the_users_own_folders(self):
        common = read("common.ps1")
        self.assertIn("$XrnHome      = Join-Path $env:LOCALAPPDATA 'xrnmoni'", common)
        self.assertIn("$XrnConfDir   = Join-Path $env:APPDATA 'xrnmoni'", common)

    def test_python_found_by_full_path_after_winget(self):
        # winget adds Python to PATH in the registry, not in the running window, so the old
        # installer's next `py -3` failed. Now the PATH is read again and Python is found by
        # its full path (py launcher, PATH, or the per-user install folder).
        text = read("install.ps1")
        winget = text.index("winget.exe")
        self.assertGreater(text.index("Update-PathFromRegistry", winget), winget)
        self.assertIn("'--scope', 'user'", text)
        self.assertIn("Programs\\Python", text)
        self.assertNotRegex(text, r"(?m)^\s*&\s*py\s")   # never run a bare `py` and hope

    def test_asks_for_hub_token_and_name(self):
        text = read("install.ps1")
        for param in ("[string]$HubUrl", "[string]$DeviceToken", "[string]$DeviceName"):
            self.assertIn(param, text)
        self.assertIn("-AsSecureString", text)   # the token isn't echoed while typed


class BrowserBridgeTest(unittest.TestCase):
    """The agent takes the extension's posts only with bridge_secret and from bridge_origins,
    so the installer has to write both, or website time is never recorded."""

    def test_installer_writes_a_secret_and_the_origins(self):
        text = read("install.ps1")
        self.assertIn("if (-not $secret) { $secret = New-XrnSecret }", text)
        self.assertIn("bridge_secret = ConvertTo-TomlString $secret", text)
        self.assertIn("bridge_origins = '[' +", text)
        self.assertIn("[string[]]$BridgeOrigin", text)

    def test_extension_is_loaded_from_a_folder_that_stays_put(self):
        self.assertIn("$XrnExtension = Join-Path $XrnHome 'extension'", read("common.ps1"))
        text = read("install.ps1")
        self.assertIn("Copy-Item -Recurse -Path $extSource -Destination $XrnExtension", text)

    def test_installer_lists_the_id_the_manifest_key_gives(self):
        # The manifest has a "key", so Chrome, Edge and Brave name the extension after that
        # key wherever its folder is. The installer listed a hash of the folder's path
        # instead, an origin the browser never uses, and every post was refused with 403.
        common = read("common.ps1")
        body = common[common.index("function Get-XrnExtensionId"):]
        body = body[:body.index("\n}") + 2]
        self.assertIn("ConvertFrom-Json", body)
        self.assertIn("[Convert]::FromBase64String($manifest.key)", body)
        self.assertIn("SHA256", body)
        self.assertNotIn("GetFullPath", body)           # not the folder's path
        text = read("install.ps1")
        self.assertIn("$chromium = 'chrome-extension://' + (Get-XrnExtensionId $XrnExtension)", text)
        self.assertNotIn("Get-UnpackedExtensionId", common + text)
        self.assertIn(f"`chrome-extension://{chromium_id()}`", read("README-WINDOWS.md"))

    def test_ci_posts_from_the_origin_chrome_really_uses(self):
        # The windows job used to pass the origin it then posted from (-BridgeOrigin), so it
        # passed whatever the installer listed for the extension itself.
        spec = importlib.util.spec_from_file_location("ci_check", os.path.join(WINDOWS, "ci", "check.py"))
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        self.assertEqual(check.chromium_origin(), "chrome-extension://" + chromium_id())
        with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            ci = f.read()
        self.assertNotRegex(ci, r"-BridgeOrigin\s+['\"]?chrome-extension://")
        self.assertIn("check.py bridge", ci)


class HubOptionsTest(unittest.TestCase):
    """-WithHub runs server/xrnmoni.py with options the scripts and the guide name."""

    def hub_options(self):
        found = set()
        for sub in ([], ["user"], ["token"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "server", "xrnmoni.py"), *sub, "--help"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(0, r.returncode, r.stderr)
            found |= set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", r.stdout))
        return found

    def test_every_hub_option_mentioned_exists(self):
        # install.ps1 and the guide told people to add -HubArgs '--insecure-cookie', which the
        # hub doesn't have: argparse exits with 2, so the hub task never came up.
        mentioned = {}
        paths = scripts("*.ps1", "*.bat", "*.md") + [os.path.join(ROOT, ".github", "workflows", "ci.yml")]
        for path in paths:
            with open(path, encoding="utf-8-sig") as f:
                for n, line in enumerate(f, 1):
                    if re.search(r"(?i)xrnmoni\.py|XrnHubScript|HubArgs", line):
                        for opt in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", line):
                            mentioned.setdefault(opt, []).append(f"{os.path.basename(path)}:{n}")
        self.assertIn("--data", mentioned)   # the scan sees them
        known = self.hub_options()
        self.assertEqual({}, {o: where for o, where in mentioned.items() if o not in known})


class LogonTaskTest(unittest.TestCase):
    """The logon task: this user only, not elevated, no window, no time limit, restarts."""

    def setUp(self):
        text = read("install.ps1")
        self.task = text[text.index("function Register-XrnTask"):]

    def test_runs_as_this_user_when_logged_on_without_elevation(self):
        self.assertIn("-LogonType Interactive -RunLevel Limited", self.task)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn -User $user", self.task)

    def test_no_time_limit_and_comes_back_after_a_crash(self):
        for setting in ("-ExecutionTimeLimit ([TimeSpan]::Zero)", "-RestartCount 999",
                        "-MultipleInstances IgnoreNew", "-RepetitionInterval (New-TimeSpan -Minutes 5)",
                        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"):
            self.assertIn(setting, self.task)

    def test_starts_pythonw_with_the_launcher_not_a_console(self):
        self.assertIn("$agentExe = $XrnPythonw", self.task)
        self.assertIn("$XrnPythonw   = Join-Path $XrnVenv 'Scripts\\pythonw.exe'", read("common.ps1"))
        self.assertIn("$agentArgs = \"`\"$XrnLauncher`\" --config `\"$XrnConfig`\"\"", self.task)
        self.assertNotRegex(self.task, r"(?i)-Execute\s+['\"]?powershell")


class StopOnlyOurOwnTest(unittest.TestCase):
    """stop and uninstall end xrnmoni's processes and nothing else."""

    def test_never_by_image_name(self):
        # `taskkill /f /im pythonw.exe` ended every Python program on the PC.
        for path in scripts("*.ps1", "*.bat"):
            text = "".join(line for _, line in code_lines(path))
            with self.subTest(script=os.path.basename(path)):
                self.assertNotRegex(text, r"(?i)taskkill[^\n]*/im\b")
                self.assertNotRegex(text, r"(?i)Stop-Process\s+-Name|Get-Process\s+[\w*]*python[^\n]*\|\s*Stop-Process")

    def test_processes_are_ours_by_exact_path(self):
        common = read("common.ps1")
        body = common[common.index("function Get-XrnProcesses"):common.index("function Test-SameProcess")]
        self.assertIn("$exe -ieq $XrnExe", body)
        self.assertIn("Test-XrnPath $cmd $XrnLauncher", body)
        self.assertIn("Test-XrnPath $cmd $XrnHubScript", body)
        stop = common[common.index("function Stop-XrnProcesses"):]
        self.assertIn("'taskkill.exe' @('/pid'", stop)          # politely, by id
        self.assertIn("if (Test-SameProcess $p)", stop)         # never a reused id
        for name in ("stop.ps1", "uninstall.ps1"):
            self.assertIn("Stop-XrnProcesses", read(name))

    def test_stop_keeps_the_task_from_starting_it_again(self):
        text = read("stop.ps1")
        self.assertLess(text.index("Disable-ScheduledTask"), text.index("Stop-XrnProcesses"))
        self.assertIn("Enable-ScheduledTask", read("start.ps1"))

    def test_uninstall_removes_tasks_program_and_venv_and_data_only_when_asked(self):
        text = read("uninstall.ps1")
        self.assertIn("Unregister-ScheduledTask", text)
        self.assertIn("$gone = @($XrnApp, $XrnVenv, $XrnBin, $XrnExtension, $XrnAgentPid)", text)
        self.assertIn("if ($RemoveData) { $gone = @($XrnHome, $XrnConfDir) }", text)
        self.assertIn("Set-Location $env:TEMP", text)

    def test_scripts_are_ascii(self):
        # Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page.
        for path in scripts("*.ps1", "*.bat"):
            with self.subTest(script=os.path.basename(path)):
                with open(path, "rb") as f:
                    f.read().decode("ascii")


class ExeTest(unittest.TestCase):
    """xrnmoni-agent.exe: the agent with Python inside, so the PC needs none. CI builds it."""

    def test_spec_builds_a_windowed_launcher(self):
        spec = read("xrnmoni-agent.spec")
        compile(spec, "xrnmoni-agent.spec", "exec")
        self.assertIn('[os.path.join(SPECPATH, "launcher.py")]', spec)   # logs, pid, clean stop
        self.assertIn('pathex=[os.path.join(ROOT, "agent")]', spec)
        self.assertIn("console=False", spec)
        self.assertIn('name="xrnmoni-agent"', spec)
        self.assertIn("COLLECT(", spec)   # one folder: no unpacking to %TEMP%, one process

    def test_build_script_runs_pyinstaller_on_the_spec(self):
        text = read("build-exe.ps1")
        self.assertIn("-m PyInstaller", text)
        self.assertIn("'xrnmoni-agent.spec'", text)

    def test_the_package_carries_the_admin_scripts(self):
        # private-windows.ps1 lives in windows/admin/, so the package a parent copies to the child's
        # PC must include it; the top-level file copy above would miss the subfolder.
        self.assertTrue(os.path.isfile(os.path.join(WINDOWS, "admin", "private-windows.ps1")))
        text = read("build-exe.ps1")
        self.assertRegex(text, r"Join-Path \$PSScriptRoot 'admin'")
        self.assertRegex(text, r"Copy-Item -Recurse \$admin")

    def test_installer_can_use_the_exe_instead_of_python(self):
        text = read("install.ps1")
        self.assertIn("[switch]$UseExe", text)
        self.assertIn("$needPython = (-not $UseExe) -or $WithHub", text)
        self.assertIn("$agentExe = $XrnExe", text)
        self.assertIn("$agentArgs = \"--config `\"$XrnConfig`\"\"", text)
        self.assertIn("$XrnExe       = Join-Path $XrnBin 'xrnmoni-agent.exe'", read("common.ps1"))


if __name__ == "__main__":
    unittest.main()
