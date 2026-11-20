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
# (-Reconfigure; -BridgeOrigin <origin> adds a browser's extension). Every question can be
# answered on the command line instead:
#
#   install.ps1 -HubUrl https://hub.example.net/xrnmoni -DeviceToken xrnmoni_... -DeviceName den-pc
#
# -UseExe installs xrnmoni-agent.exe (windows\build-exe.ps1, or CI's build) instead of Python
# and a venv: the PC then needs no Python. It looks for xrnmoni-agent\xrnmoni-agent.exe beside
# windows\ or in windows\dist\, or takes -ExePath.
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
    [string[]]$BridgeOrigin,
    [string]$Python,
    [switch]$UseExe,
    [string]$ExePath,
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

# The package (build-exe.ps1) has the exe and no agent\agent.py: use the exe without being told.
if (-not $UseExe -and -not (Test-Path (Join-Path $src 'agent\agent.py')) -and
    ((Test-Path (Join-Path $src 'xrnmoni-agent\xrnmoni-agent.exe')) -or (Test-Path $XrnExe))) {
    $UseExe = $true
}
$needPython = (-not $UseExe) -or $WithHub
$python = $null
if ($needPython) { Step 'Python'; $python = Find-Python }
if ($needPython -and -not $python) {
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
if ($python) { Write-Host "Using $python" }

$exeSource = $null
if ($UseExe) {
    $tries = @($ExePath, (Join-Path $src 'xrnmoni-agent\xrnmoni-agent.exe'),
               (Join-Path $PSScriptRoot 'dist\xrnmoni-agent\xrnmoni-agent.exe'), $XrnExe)
    $exeSource = $tries | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $exeSource) { Fail 'No xrnmoni-agent.exe found: build it with windows\build-exe.ps1, or pass -ExePath.' }
}

# --- 2. Program files and venv ---------------------------------------------------------------
Step 'Program'
# An update: turn the tasks off (registering them again below turns them on), or their
# 5-minute repetition could start the agent again while its files are being replaced.
foreach ($t in @(Get-XrnTasks)) { Disable-ScheduledTask -TaskName $t.TaskName | Out-Null }
if (@(Get-XrnProcesses).Count -gt 0) {
    Write-Host 'Stopping the running copy first...'
    Stop-XrnProcesses | Out-Null
}
New-Item -ItemType Directory -Force -Path $XrnHome, $XrnLogs | Out-Null

$fromInstalled = (Resolve-Path $src).Path.TrimEnd('\') -ieq $XrnApp.TrimEnd('\')
if (-not $fromInstalled) {
    $dirs = @('windows')
    if (-not $UseExe) { $dirs += 'agent' }
    if (-not $UseExe -and -not (Test-Path (Join-Path $src 'agent\agent.py'))) { Fail "No agent\agent.py next to $PSScriptRoot." }
    if (Test-Path $XrnApp) { Remove-Item -Recurse -Force $XrnApp }
    foreach ($d in $dirs) {   # their files only: not windows\build, windows\dist
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
# The extension gets a folder of its own that stays put across updates, so the browser
# keeps loading it from the same place.
$extSource = Join-Path $src 'extension'
if ((Test-Path (Join-Path $extSource 'manifest.json')) -and
    (Resolve-Path $extSource).Path.TrimEnd('\') -ine $XrnExtension.TrimEnd('\')) {
    if (Test-Path $XrnExtension) { Remove-Item -Recurse -Force $XrnExtension }
    Copy-Item -Recurse -Path $extSource -Destination $XrnExtension
}
if ($WithHub -and -not (Test-Path $XrnHubScript)) { Fail "No server\xrnmoni.py in ${src}, so this copy has no hub." }

if ($UseExe) {
    $exeDir = Split-Path $exeSource -Parent     # the exe and its _internal folder
    if ((Resolve-Path $exeDir).Path.TrimEnd('\') -ine $XrnBin.TrimEnd('\')) {
        if (Test-Path $XrnBin) { Remove-Item -Recurse -Force $XrnBin }
        Copy-Item -Recurse -Path $exeDir -Destination $XrnBin
    }
    Write-Host "Installed $XrnExe"
} elseif (Test-Path $XrnBin) {
    Remove-Item -Recurse -Force $XrnBin        # an earlier -UseExe install
}

if ($needPython) {
    if ((Test-Path $XrnVenvPy) -and -not (Test-PythonExe $XrnVenvPy)) {
        Write-Host 'The venv no longer works (its Python was removed or upgraded); making it again.'
        Remove-Item -Recurse -Force $XrnVenv
    }
    if (-not (Test-Path $XrnVenvPy)) {
        & $python -m venv $XrnVenv
        if ($LASTEXITCODE -ne 0) { Fail "Could not make a venv in $XrnVenv." }
    }
    $reqs = @()
    if (-not $UseExe) { $reqs += @('-r', (Join-Path $XrnApp 'agent\requirements.txt')) }
    if ($WithHub) { $reqs += @('-r', (Join-Path $XrnApp 'server\requirements.txt')) }
    & $XrnVenvPy -m pip install --disable-pip-version-check --upgrade @reqs
    if ($LASTEXITCODE -ne 0) { Fail 'Installing the Python packages failed (see above). Is this PC online?' }
} elseif (Test-Path $XrnVenv) {
    Remove-Item -Recurse -Force $XrnVenv       # an earlier install without -UseExe
}

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
function Read-Plain([string]$Prompt, [string]$Default) {
    $shown = $Prompt
    if ($Default) { $shown = "$Prompt [$Default]" }
    $v = Read-Host $shown
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    $v.Trim()
}

# Ask only for what isn't known yet, or for everything with -Reconfigure; what the command
# line gives wins. Settings added to the file by hand are kept.
$ask = $Reconfigure -or -not (Test-Path -LiteralPath $XrnConfig)
$url = Read-XrnConfigValue 'server_url'
$device = Read-XrnConfigValue 'device'
$token = Read-XrnConfigValue 'token'
if ($HubUrl) { $url = $HubUrl }
elseif ($WithHub -and ($ask -or -not $url)) { $url = "http://127.0.0.1:${HubPort}" }
elseif ($ask -or -not $url) { $url = Read-Plain 'Hub address (e.g. https://hub.example.net/xrnmoni)' $url }
$uri = $null
if (-not $url -or -not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or
    ($uri.Scheme -ne 'https' -and $uri.Scheme -ne 'http')) {
    Fail "'$url' is not an http:// or https:// address."
}
$url = $url.TrimEnd('/')
if ($uri.Scheme -eq 'http' -and -not $uri.IsLoopback) {
    Write-Host ('That address is plain HTTP, so the device token crosses the network unencrypted. ' +
                "Use the hub's https:// address if it has one (Tailscale encrypts on its own).") -ForegroundColor Yellow
}
if ($DeviceName) { $device = $DeviceName }
elseif ($ask -or -not $device) {
    if (-not $device) { $device = $env:COMPUTERNAME }
    $device = Read-Plain 'Name for this PC on the dashboard' $device
}
if ($DeviceToken) { $token = $DeviceToken }
elseif ($WithHub -and ($ask -or -not $token)) {
    $r = Invoke-Native $XrnVenvPy @($XrnHubScript, 'token', 'add', $device, '--data', $XrnHubData)
    if ($r.Code -ne 0) { Fail "Could not make a device token: $($r.Output)" }
    $token = (($r.Output -split "`n") | Where-Object { $_ -match '^xrnmoni_' } | Select-Object -First 1)
    Write-Host "Made a device token for '$device' on this PC's hub."
} elseif ($ask -or -not $token) {
    $prompt = "Device token for '$device' (on the hub: xrnmoni.py token add $device)"
    if ($token) { $prompt += ' [Enter keeps the current one]' }
    $secure = Read-Host $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $typed = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($typed) { $token = $typed }
}
if (-not $token) { Fail 'A device token is needed: make one on the hub with  xrnmoni.py token add <name>.' }

# The browser extension posts to the agent on 127.0.0.1 with this secret, and only from the
# origins listed. Chrome, Edge and Brave name it after the public key in its manifest, so its
# origin is known; Firefox's is random per profile and is added with -BridgeOrigin (the
# extension's options page shows it).
$secret = Read-XrnConfigValue 'bridge_secret'
if (-not $secret) { $secret = New-XrnSecret }
$origins = New-Object System.Collections.Generic.List[string]
foreach ($o in @(Read-XrnConfigArray 'bridge_origins') + @($BridgeOrigin)) {
    if ($o -and -not $origins.Contains($o.Trim())) { $origins.Add($o.Trim()) }
}
if (Test-Path -LiteralPath (Join-Path $XrnExtension 'manifest.json')) {
    $chromium = 'chrome-extension://' + (Get-XrnExtensionId $XrnExtension)
    if ($chromium -ne 'chrome-extension://' -and -not $origins.Contains($chromium)) { $origins.Add($chromium) }
}
$values = [ordered]@{
    server_url = ConvertTo-TomlString $url
    token = ConvertTo-TomlString $token
    device = ConvertTo-TomlString $device
    bridge_secret = ConvertTo-TomlString $secret
    bridge_origins = '[' + (($origins | ForEach-Object { ConvertTo-TomlString $_ }) -join ', ') + ']'
}
Write-XrnConfig $values
Write-Host "Wrote $XrnConfig"

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
if ($UseExe) {
    $agentExe = $XrnExe
    $agentArgs = "--config `"$XrnConfig`""
}
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
Write-Host ''
Write-Host "  Browser extension: load $XrnExtension unpacked (windows\README-WINDOWS.md says how)," -ForegroundColor Cyan
Write-Host "  then paste this into its options page as the Secret:  $(Read-XrnConfigValue 'bridge_secret')" -ForegroundColor Cyan
if ($WithHub) {
    Write-Host "  Hub log: $(Join-Path $XrnHubData 'xrnmoni.log')"
    Write-Host "  Sign-in for the dashboard (once):  & `"$XrnVenvPy`" `"$XrnHubScript`" user add <name> --data `"$XrnHubData`""
}
Write-Host '  Check on it any time with windows\status.bat.'
if (-not $started) { exit 1 }
exit 0
