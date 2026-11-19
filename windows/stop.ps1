# Stop xrnmoni, and keep it stopped (also at the next logon) until start.ps1 runs.
# stop-xrnmoni.bat runs this. Only xrnmoni's own processes are stopped: the agent is asked
# to close first, so it writes what it has, and ended outright only if it doesn't.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

foreach ($t in @(Get-XrnTasks)) {
    # Disabled first, or its 5-minute repetition would start the agent again.
    Disable-ScheduledTask -TaskName $t.TaskName | Out-Null
    Write-Host "Task '$($t.TaskName)' turned off (start.ps1 turns it back on)."
}
$n = Stop-XrnProcesses
if ($n -gt 0) { Write-Host "xrnmoni stopped ($n processes)." -ForegroundColor Green }
else { Write-Host 'xrnmoni was not running.' }
if (Test-Path -LiteralPath $XrnStartup) {
    Write-Host 'It starts again at the next logon (Startup-folder shortcut); uninstall.ps1 removes that.'
}
