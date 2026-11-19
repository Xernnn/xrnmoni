# Remove xrnmoni for this user: stops it, removes its logon tasks, program and venv.
# Keeps the settings, logs, unsent events and any hub data, unless -RemoveData.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-RemoveData]
[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location $env:TEMP   # this script may be inside the folder it deletes

foreach ($t in @(Get-XrnTasks)) {
    Disable-ScheduledTask -TaskName $t.TaskName | Out-Null
}
$n = Stop-XrnProcesses
if ($n -gt 0) { Write-Host "Stopped xrnmoni ($n processes)." }
foreach ($t in @(Get-XrnTasks)) {
    Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    Write-Host "Removed task '$($t.TaskName)'."
}
if (Test-Path -LiteralPath $XrnStartup) { Remove-Item -LiteralPath $XrnStartup -Force }

$gone = @($XrnApp, $XrnVenv, $XrnBin, $XrnAgentPid)
if ($RemoveData) { $gone = @($XrnHome, $XrnConfDir) }
foreach ($p in $gone) {
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force
        Write-Host "Removed $p"
    }
}
if ($RemoveData) {
    Write-Host 'xrnmoni is removed, with its settings, logs and unsent events.' -ForegroundColor Green
} else {
    Write-Host 'xrnmoni is removed. Kept (uninstall.ps1 -RemoveData removes these too):' -ForegroundColor Green
    Write-Host "  settings:                $XrnConfig"
    Write-Host "  logs, unsent events, hub: $XrnHome"
}
exit 0
