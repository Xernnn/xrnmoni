# Start xrnmoni now, and at every logon again if stop.ps1 turned that off.
# start-xrnmoni.bat runs this.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$tasks = @(Get-XrnTasks)
if ($tasks.Count -gt 0) {
    foreach ($t in $tasks) {
        Enable-ScheduledTask -TaskName $t.TaskName | Out-Null
        Start-ScheduledTask -TaskName $t.TaskName   # refused quietly if it is already running
    }
} elseif (Test-Path -LiteralPath $XrnStartup) {
    if (@(Get-XrnProcesses -Role agent).Count -eq 0) { Invoke-Item -LiteralPath $XrnStartup }
} else {
    Write-Host 'xrnmoni is not installed for this user: run install.ps1 first.' -ForegroundColor Red
    exit 1
}

# The agent appears within a few seconds; say so, or where to look.
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline -and @(Get-XrnProcesses -Role agent).Count -eq 0) { Start-Sleep -Milliseconds 500 }
$agent = @(Get-XrnProcesses -Role agent)
if ($agent.Count -gt 0) {
    Write-Host "xrnmoni is running (process $(($agent | ForEach-Object { $_.ProcessId }) -join ', '))." -ForegroundColor Green
} else {
    Write-Host "xrnmoni did not start. See $(Join-Path $XrnLogs 'agent.log') and run status.bat." -ForegroundColor Red
    exit 1
}
exit 0
