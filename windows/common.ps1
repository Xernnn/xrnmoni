# Shared by install.ps1, start.ps1, stop.ps1, uninstall.ps1 and status.ps1 (dot-source it).
# Written for Windows PowerShell 5.1, which Windows 10 and 11 ship, and PowerShell 7.
# Everything is per user: nothing here needs an administrator.

$XrnHome      = Join-Path $env:LOCALAPPDATA 'xrnmoni'   # program, venv, logs, spool, hub data
$XrnConfDir   = Join-Path $env:APPDATA 'xrnmoni'
$XrnConfig    = Join-Path $XrnConfDir 'config.toml'
$XrnApp       = Join-Path $XrnHome 'app'                # a copy of agent\, windows\ (and server\)
$XrnVenv      = Join-Path $XrnHome 'venv'
$XrnBin       = Join-Path $XrnHome 'bin'                # xrnmoni-agent.exe, with -UseExe
$XrnLogs      = Join-Path $XrnHome 'logs'
$XrnHubData   = Join-Path $XrnHome 'hub'
$XrnAgentPid  = Join-Path $XrnHome 'agent.pid'
$XrnPythonw   = Join-Path $XrnVenv 'Scripts\pythonw.exe'
$XrnVenvPy    = Join-Path $XrnVenv 'Scripts\python.exe'
$XrnLauncher  = Join-Path $XrnApp 'windows\launcher.py'
$XrnHubScript = Join-Path $XrnApp 'server\xrnmoni.py'
$XrnExe       = Join-Path $XrnBin 'xrnmoni-agent.exe'
$XrnAgentTask = 'xrnmoni agent'
$XrnHubTask   = 'xrnmoni hub'
$XrnStartup   = Join-Path ([Environment]::GetFolderPath('Startup')) 'xrnmoni agent.lnk'

function Get-XrnUserId {
    [Security.Principal.WindowsIdentity]::GetCurrent().Name   # PC\name, or AzureAD\Name
}

function Invoke-Native {
    # Run a program and return its exit code and output. Windows PowerShell turns a native
    # program's stderr into errors (fatal under -ErrorAction Stop) once it is redirected, so
    # the preference is relaxed for the call.
    param([string]$Exe, [string[]]$Arguments = @())
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Code = -1; Output = "$Exe not found" }
    }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    [pscustomobject]@{ Code = $code; Output = (@($out) -join "`n") }
}

function Test-XrnPath([string]$Text, [string]$Path) {
    $Text -and $Path -and $Text.IndexOf($Path, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-XrnProcesses {
    # xrnmoni's own processes, and nothing else: the agent is our pythonw running our
    # launcher.py, or our xrnmoni-agent.exe; the hub is a python running our server script.
    # Never "every pythonw.exe": other programs use Python too.
    param([ValidateSet('agent', 'hub', 'all')] [string]$Role = 'all')
    $filter = "Name='pythonw.exe' OR Name='python.exe' OR Name='xrnmoni-agent.exe'"
    foreach ($p in @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)) {
        $cmd = [string]$p.CommandLine
        $exe = [string]$p.ExecutablePath
        $agent = ($exe -and $exe -ieq $XrnExe) -or (Test-XrnPath $cmd $XrnLauncher)
        $hub = Test-XrnPath $cmd $XrnHubScript
        if (($agent -and $Role -ne 'hub') -or ($hub -and $Role -ne 'agent')) { $p }
    }
}

function Test-SameProcess($Proc) {
    # The id still belongs to the process we found (ids are reused after a process ends).
    $now = Get-CimInstance Win32_Process -Filter "ProcessId=$($Proc.ProcessId)" -ErrorAction SilentlyContinue
    $now -and $now.CreationDate -eq $Proc.CreationDate
}

function Stop-XrnProcesses {
    # Stop the agent politely first: taskkill without /f closes its hidden window, and the
    # agent writes its open interval to the spool and exits. Whatever is left after
    # $TimeoutSec, and the hub (which has no window), is ended outright.
    param([ValidateSet('agent', 'hub', 'all')] [string]$Role = 'all', [int]$TimeoutSec = 20)
    $procs = @(Get-XrnProcesses -Role $Role)
    if ($procs.Count -eq 0) { return 0 }
    $polite = @($procs | Where-Object { -not (Test-XrnPath ([string]$_.CommandLine) $XrnHubScript) })
    foreach ($p in $polite) { Invoke-Native 'taskkill.exe' @('/pid', "$($p.ProcessId)") | Out-Null }
    if ($polite.Count -gt 0) {
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline -and @($polite | Where-Object { Test-SameProcess $_ }).Count -gt 0) {
            Start-Sleep -Milliseconds 500
        }
    }
    foreach ($p in $procs) {
        if (Test-SameProcess $p) {
            Write-Host "  ending process $($p.ProcessId) ($($p.Name)) outright"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    return $procs.Count
}

function Get-XrnTasks {
    foreach ($name in @($XrnAgentTask, $XrnHubTask)) {
        Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    }
}

function ConvertTo-TomlString([string]$Text) {
    # A TOML basic string: quote and backslash escaped, control characters as \uXXXX.
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

function Read-XrnConfigValue([string]$Key) {
    # Good enough for the file install.ps1 writes: key = "string" on one line.
    if (-not (Test-Path -LiteralPath $XrnConfig)) { return $null }
    foreach ($line in [IO.File]::ReadAllLines($XrnConfig)) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*"((?:[^"\\]|\\.)*)"')) {
            return [regex]::Unescape($Matches[1])
        }
    }
    $null
}
