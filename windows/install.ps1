# xrnmoni installer for Windows 10 and 11: run it as the person whose activity is recorded,
# in a normal (not administrator) PowerShell. It needs no administrator rights.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# It asks for the hub's address, this PC's device token and its name, then:
#   1. finds Python 3.10+ (or installs it for this user with winget),
#   2. copies the agent to %LOCALAPPDATA%\xrnmoni\app and makes a venv beside it,
#   3. writes %APPDATA%\xrnmoni\config.toml,
#   4. registers the task "xrnmoni agent", which starts it at every logon with no window,
#   5. starts it now.
# Run it again to update (from a newer copy of the folder) or to change the settings
# (-Reconfigure). Every question can be answered on the command line instead:
#
#   install.ps1 -HubUrl https://hub.example.net/xrnmoni -DeviceToken xrnmoni_... -DeviceName den-pc
#
# -WithHub also runs a hub on this PC (python server\xrnmoni.py, listening on 127.0.0.1:5240
# only; -HubBind <address> to let other machines in, -HubArgs '--insecure-cookie ...' for more
# of its options) and points the agent at it.

[CmdletBinding()]
param(
    [string]$HubUrl,
    [string]$DeviceToken,
    [string]$DeviceName,
    [switch]$Reconfigure,
    [switch]$WithHub,
    [string]$HubBind = '127.0.0.1',
    [int]$HubPort = 5240,
    [string]$HubArgs = '',
    [string]$Python,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$src = Split-Path $PSScriptRoot -Parent      # the xrnmoni folder this script came from

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Fail([string]$Text) { Write-Host $Text -ForegroundColor Red; exit 1 }

# --- 1. Python -------------------------------------------------------------------------------
function Test-PythonExe([string]$Path) {
    # A real Python 3.10+, not the Microsoft Store's python.exe stub (or the Store's Python,
    # which keeps what it writes under %LOCALAPPDATA% in a private copy of its own).
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ($Path -like '*\WindowsApps\*') { return $false }
    $r = Invoke-Native $Path @('-c', 'import sys; print(sys.version_info >= (3, 10))')
    return ($r.Code -eq 0 -and $r.Output.Trim() -eq 'True')
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Python) { $candidates.Add($Python) }
    foreach ($py in @('py.exe', (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'))) {
        $r = Invoke-Native $py @('-3', '-c', 'import sys; print(sys.executable)')
        if ($r.Code -eq 0) { $candidates.Add(($r.Output -split "`n")[-1].Trim()) }
    }
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) { $candidates.Add($cmd.Source) }
    $dirs = @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)
    foreach ($dir in $dirs) {
        if ($dir -and (Test-Path -LiteralPath $dir)) {
            Get-ChildItem -LiteralPath $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object { [int](($_.Name -replace '\D', '') + '0') } -Descending |
                ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    foreach ($c in $candidates) { if (Test-PythonExe $c) { return $c } }
    return $null
}

function Update-PathFromRegistry {
    # winget's installer adds Python to the user's PATH in the registry; this window still has
    # the PATH it started with, so read it again.
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User')) | Where-Object { $_ }
    $env:Path = $parts -join ';'
}

Step 'Python'
$python = Find-Python
if (-not $python) {
    Write-Host 'No Python 3.10 or newer found. Installing Python 3.13 for this user with winget...'
    $r = Invoke-Native 'winget.exe' @('install', '--id', 'Python.Python.3.13', '--exact', '--scope', 'user',
        '--silent', '--accept-package-agreements', '--accept-source-agreements')
    Write-Host $r.Output
    Update-PathFromRegistry
    $python = Find-Python
    if (-not $python) {
        Fail ("Python could not be installed automatically. Install it from https://www.python.org/downloads/ " +
              "(the default 'Install Now' is per user and needs no administrator), then run this again.")
    }
}
Write-Host "Using $python"

# --- 2. Program files and venv ---------------------------------------------------------------
Step 'Program'
$running = @(Get-XrnProcesses)
if ($running.Count -gt 0) {
    Write-Host 'Stopping the running copy first...'
    Stop-XrnProcesses | Out-Null
}
New-Item -ItemType Directory -Force -Path $XrnHome, $XrnLogs | Out-Null

$fromInstalled = (Resolve-Path $src).Path.TrimEnd('\') -ieq $XrnApp.TrimEnd('\')
if (-not $fromInstalled) {
    if (-not (Test-Path (Join-Path $src 'agent\agent.py'))) { Fail "No agent\agent.py next to $PSScriptRoot." }
    if (Test-Path $XrnApp) { Remove-Item -Recurse -Force $XrnApp }
    foreach ($d in @('agent', 'windows')) {   # their files only: not windows\build, windows\dist
        New-Item -ItemType Directory -Force -Path (Join-Path $XrnApp $d) | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $src $d) -File |
            Where-Object { $_.Name -ne 'config.toml' -and $_.Name -ne 'token.txt' } |
            Copy-Item -Destination (Join-Path $XrnApp $d)
    }
    if ($WithHub -and (Test-Path (Join-Path $src 'server'))) {
        Copy-Item -Recurse -Path (Join-Path $src 'server') -Destination (Join-Path $XrnApp 'server')
        Get-ChildItem -LiteralPath (Join-Path $XrnApp 'server') -Recurse -Force -Directory -Filter '__pycache__' |
            Remove-Item -Recurse -Force
    }
    Write-Host "Copied to $XrnApp"
}
if ($WithHub -and -not (Test-Path $XrnHubScript)) { Fail "No server\xrnmoni.py in ${src}, so this copy has no hub." }

if ((Test-Path $XrnVenvPy) -and -not (Test-PythonExe $XrnVenvPy)) {
    Write-Host 'The venv no longer works (its Python was removed or upgraded); making it again.'
    Remove-Item -Recurse -Force $XrnVenv
}
if (-not (Test-Path $XrnVenvPy)) {
    & $python -m venv $XrnVenv
    if ($LASTEXITCODE -ne 0) { Fail "Could not make a venv in $XrnVenv." }
}
$reqs = @('-r', (Join-Path $XrnApp 'agent\requirements.txt'))
if ($WithHub) { $reqs += @('-r', (Join-Path $XrnApp 'server\requirements.txt')) }
& $XrnVenvPy -m pip install --disable-pip-version-check --upgrade @reqs
if ($LASTEXITCODE -ne 0) { Fail 'Installing the Python packages failed (see above). Is this PC online?' }

# --- 3. Hub on this PC (optional) ------------------------------------------------------------
if ($WithHub) {
    Step 'Hub on this PC'
    New-Item -ItemType Directory -Force -Path $XrnHubData | Out-Null
    if ($HubBind -ne '127.0.0.1' -and $HubBind -ne 'localhost' -and $HubBind -ne '::1') {
        Write-Host ("The hub will listen on ${HubBind}:${HubPort}, so other machines can reach it. Windows " +
                    'Firewall asks an administrator the first time; allow it only for private networks. ' +
                    'It speaks plain HTTP: use it over Tailscale or put an HTTPS proxy in front. To sign in ' +
                    "to its dashboard over plain HTTP it needs -HubArgs '--insecure-cookie'.") -ForegroundColor Yellow
    }
}

# --- 4. Config -------------------------------------------------------------------------------
Step 'Settings'
function ConvertTo-TomlString([string]$Text) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq [char]'"') { [void]$sb.Append('\"') }
        elseif ($ch -eq [char]'\') { [void]$sb.Append('\\') }
        elseif ($code -lt 0x20 -or $code -eq 0x7f) { [void]$sb.Append(('\u{0:X4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    $sb.ToString()
}

function Read-Plain([string]$Prompt, [string]$Default) {
    $shown = $Prompt
    if ($Default) { $shown = "$Prompt [$Default]" }
    $v = Read-Host $shown
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    $v.Trim()
}

$haveConfig = Test-Path -LiteralPath $XrnConfig
if ($haveConfig -and -not $Reconfigure -and -not $HubUrl -and -not $DeviceToken -and -not $DeviceName) {
    Write-Host "Keeping $XrnConfig (run with -Reconfigure to change it)."
} else {
    if (-not $HubUrl -and $WithHub) { $HubUrl = "http://127.0.0.1:${HubPort}" }
    if (-not $HubUrl) { $HubUrl = Read-Plain "Hub address (e.g. https://hub.example.net/xrnmoni)" (Read-XrnConfigValue 'server_url') }
    $uri = $null
    if (-not $HubUrl -or -not [Uri]::TryCreate($HubUrl, [UriKind]::Absolute, [ref]$uri) -or
        ($uri.Scheme -ne 'https' -and $uri.Scheme -ne 'http')) {
        Fail "'$HubUrl' is not an http:// or https:// address."
    }
    $HubUrl = $HubUrl.TrimEnd('/')
    if ($uri.Scheme -eq 'http' -and -not $uri.IsLoopback) {
        Write-Host ('That address is plain HTTP, so the device token crosses the network unencrypted. ' +
                    "Use the hub's https:// address if it has one (Tailscale encrypts on its own).") -ForegroundColor Yellow
    }
    if (-not $DeviceName) {
        $current = Read-XrnConfigValue 'device'
        if (-not $current) { $current = $env:COMPUTERNAME }
        $DeviceName = Read-Plain 'Name for this PC on the dashboard' $current
    }
    if ($WithHub -and -not $DeviceToken) {
        $r = Invoke-Native $XrnVenvPy @($XrnHubScript, 'token', 'add', $DeviceName, '--data', $XrnHubData)
        if ($r.Code -ne 0) { Fail "Could not make a device token: $($r.Output)" }
        $DeviceToken = (($r.Output -split "`n") | Where-Object { $_ -match '^xrnmoni_' } | Select-Object -First 1)
        Write-Host "Made a device token for '$DeviceName' on this PC's hub."
    }
    if (-not $DeviceToken) {
        $old = Read-XrnConfigValue 'token'
        $prompt = "Device token for '$DeviceName' (from the hub: xrnmoni.py token add $DeviceName)"
        if ($old) { $prompt += ' [keep the current one]' }
        $secure = Read-Host $prompt -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $DeviceToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if (-not $DeviceToken) { $DeviceToken = $old }
    }
    if (-not $DeviceToken) { Fail 'A device token is needed: make one on the hub with  xrnmoni.py token add <name>.' }

    $lines = @(
        '# Written by windows\install.ps1. Run it again with -Reconfigure to change these;',
        '# any other setting in agent\config.example.toml can be added below.',
        "server_url = $(ConvertTo-TomlString $HubUrl)",
        "token = $(ConvertTo-TomlString $DeviceToken)",
        "device = $(ConvertTo-TomlString $DeviceName)"
    )
    New-Item -ItemType Directory -Force -Path $XrnConfDir | Out-Null
    # UTF-8 without a byte-order mark: Python's TOML reader refuses one, and Windows
    # PowerShell's Set-Content would write one (or the ANSI code page) instead.
    [IO.File]::WriteAllText($XrnConfig, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Wrote $XrnConfig"
}

# --- 5. Start at logon -----------------------------------------------------------------------
Step 'Start at logon'
function Register-XrnTask([string]$Name, [string]$Execute, [string]$Arguments, [string]$Description) {
    $user = Get-XrnUserId
    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $XrnHome
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Task Scheduler's "restart on failure" only covers a failure to start. Repeating the
    # logon trigger every 5 minutes, with a second copy refused (IgnoreNew), is what brings
    # the agent back after it crashes.
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description $Description -Force | Out-Null
}

# The first installer registered "xrnmoni" to run elevated; that one needs an administrator to remove.
if (Get-ScheduledTask -TaskName 'xrnmoni' -ErrorAction SilentlyContinue) {
    try {
        Unregister-ScheduledTask -TaskName 'xrnmoni' -Confirm:$false
        Write-Host 'Removed the old "xrnmoni" task.'
    } catch {
        Write-Host ('The old elevated "xrnmoni" task is still there. In an administrator terminal run: ' +
                    'schtasks /delete /tn xrnmoni /f') -ForegroundColor Yellow
    }
}
$oldRule = $null
try { $oldRule = Get-NetFirewallRule -DisplayName 'xrnmoni dashboard' -ErrorAction SilentlyContinue } catch {}
if ($oldRule) {
    Write-Host ("The old installer's firewall rule is still there. In an administrator terminal run: " +
                "Remove-NetFirewallRule -DisplayName 'xrnmoni dashboard'") -ForegroundColor Yellow
}

$agentExe = $XrnPythonw
$agentArgs = "`"$XrnLauncher`" --config `"$XrnConfig`""
$tasks = @()
try {
    Register-XrnTask $XrnAgentTask $agentExe $agentArgs 'xrnmoni: records which app is in front, for this user.'
    $tasks += $XrnAgentTask
    if ($WithHub) {
        $hubCmd = "`"$XrnHubScript`" --host $HubBind --port $HubPort --data `"$XrnHubData`" $HubArgs".TrimEnd()
        Register-XrnTask $XrnHubTask $XrnPythonw $hubCmd 'xrnmoni hub: keeps what the agents report.'
        $tasks += $XrnHubTask
    }
    if (Test-Path -LiteralPath $XrnStartup) { Remove-Item -LiteralPath $XrnStartup -Force }
    Write-Host "Registered: $($tasks -join ', ') (at logon, no window, as $(Get-XrnUserId), not elevated)."
} catch {
    # Some managed PCs don't let users make tasks. A Startup-folder shortcut still starts the
    # agent at logon, but nothing restarts it if it stops.
    Write-Host "Could not register the logon task ($($_.Exception.Message)). Using a Startup-folder shortcut instead." -ForegroundColor Yellow
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($XrnStartup)
    $lnk.TargetPath = $agentExe
    $lnk.Arguments = $agentArgs
    $lnk.WorkingDirectory = $XrnHome
    $lnk.Save()
}

# --- 6. Start now ----------------------------------------------------------------------------
if ($NoStart) {
    Write-Host "`nInstalled. It starts at the next logon, or now with windows\start-xrnmoni.bat." -ForegroundColor Green
    exit 0
}
Step 'Starting'
& (Join-Path $PSScriptRoot 'start.ps1')
$started = $LASTEXITCODE -eq 0

Write-Host "`nDone." -ForegroundColor Green
Write-Host "  Hub:     $(Read-XrnConfigValue 'server_url')"
Write-Host "  Device:  $(Read-XrnConfigValue 'device')"
Write-Host "  Config:  $XrnConfig"
Write-Host "  Log:     $(Join-Path $XrnLogs 'agent.log')"
if ($WithHub) {
    Write-Host "  Hub log: $(Join-Path $XrnHubData 'xrnmoni.log')"
    Write-Host "  Sign-in for the dashboard (once):  & `"$XrnVenvPy`" `"$XrnHubScript`" user add <name> --data `"$XrnHubData`""
}
Write-Host '  Check on it any time with windows\status.bat.'
if (-not $started) { exit 1 }
