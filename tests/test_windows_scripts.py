"""Checks on the Windows install scripts that don't need Windows.

What they do on a real Windows is checked by the `windows` job in .github/workflows/ci.yml
(it installs, starts at logon, stops and uninstalls for real). Here: mistakes that read fine
but break, or that the scripts must never do again. If PowerShell (pwsh) is on PATH, every
script is also parsed.

    python -m unittest tests.test_windows_scripts
"""
import glob
import os
import re
import shutil
import subprocess
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


def read(name):
    with open(os.path.join(WINDOWS, name), encoding="utf-8-sig") as f:
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
        self.assertIn("'chrome-extension://' + (Get-UnpackedExtensionId $XrnExtension)", text)


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

    def test_installer_can_use_the_exe_instead_of_python(self):
        text = read("install.ps1")
        self.assertIn("[switch]$UseExe", text)
        self.assertIn("$needPython = (-not $UseExe) -or $WithHub", text)
        self.assertIn("$agentExe = $XrnExe", text)
        self.assertIn("$agentArgs = \"--config `\"$XrnConfig`\"\"", text)
        self.assertIn("$XrnExe       = Join-Path $XrnBin 'xrnmoni-agent.exe'", read("common.ps1"))


if __name__ == "__main__":
    unittest.main()
