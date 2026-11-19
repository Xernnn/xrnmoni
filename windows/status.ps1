# What xrnmoni is doing on this PC: its logon tasks, processes, settings, whether the hub
# answers, the browser bridge, and the end of the log. status.bat runs this.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'common.ps1')
# Windows PowerShell 5.1 may still offer only TLS 1.0 to an https:// hub.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Show([string]$Name, [string]$Value, [string]$Color = 'Gray') {
    Write-Host ('  {0,-16}{1}' -f $Name, $Value) -ForegroundColor $Color
}

Write-Host 'Start at logon'
$tasks = @(Get-XrnTasks)
if ($tasks.Count -eq 0) {
    if (Test-Path -LiteralPath $XrnStartup) { Show 'shortcut' $XrnStartup }
    else { Show 'nothing' 'not installed for this user: run install.ps1' 'Red' }
}
foreach ($t in $tasks) {
    $i = $t | Get-ScheduledTaskInfo
    # 0x41301 running, 0 finished fine, 0x41303 not run yet, 0x800710E0 refused (not logged on)
    $color = 'Gray'
    if ($t.State -eq 'Disabled') { $color = 'Yellow' }
    Show $t.TaskName ('{0}; last started {1}, result 0x{2:X}' -f $t.State, $i.LastRunTime, $i.LastTaskResult) $color
}

Write-Host 'Processes'
$procs = @(Get-XrnProcesses)
if (@(Get-XrnProcesses -Role agent).Count -eq 0) { Show 'agent' 'not running' 'Red' }
foreach ($p in $procs) { Show "pid $($p.ProcessId)" ('{0} (since {1})' -f $p.ExecutablePath, $p.CreationDate) }

Write-Host 'Settings'
$hub = Read-XrnConfigValue 'server_url'
Show 'config' $XrnConfig
Show 'hub' $hub
Show 'device' (Read-XrnConfigValue 'device')
if ($hub) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri "$hub/healthz"
        Show 'hub answers' "yes ($($r.StatusCode))" 'Green'
    } catch {
        Show 'hub answers' "no: $($_.Exception.Message)" 'Red'
    }
}

$port = 8129
if (Test-Path -LiteralPath $XrnConfig) {
    $m = Select-String -LiteralPath $XrnConfig -Pattern '^\s*bridge_port\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
$listen = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($listen.Count -gt 0) { Show 'browser bridge' "127.0.0.1:$port (pid $($listen[0].OwningProcess))" 'Green' }
else { Show 'browser bridge' "nothing listens on 127.0.0.1:$port" 'Red' }

$logFile = Join-Path $XrnLogs 'agent.log'
Write-Host "Last lines of $logFile"
if (Test-Path -LiteralPath $logFile) {
    Get-Content -LiteralPath $logFile -Tail 15 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host '  (no log yet)'
}
