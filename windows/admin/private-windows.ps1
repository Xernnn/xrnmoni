# Turns private browsing off in the Chromium browsers on this PC, for every account on it:
# Chrome's and Chromium's Incognito, Edge's InPrivate, and Brave's private and Tor windows.
# A private window is where xrnmoni sees only "Private" and the time, so on a child's PC the
# parent may close it. Each browser reads these as machine policy, which a standard (not
# administrator) account can't change; chrome://policy and edge://policy list them.
#
# Run it once, as an administrator (right-click PowerShell, Run as administrator):
#
#   powershell -ExecutionPolicy Bypass -File windows\admin\private-windows.ps1
#   powershell -ExecutionPolicy Bypass -File windows\admin\private-windows.ps1 -Undo
#   powershell -ExecutionPolicy Bypass -File windows\admin\private-windows.ps1 -Status
#
# It only writes the values below and, with -Undo, removes those same values; nothing else under
# Policies is touched. A browser takes it at its next start.

[CmdletBinding()]
param(
    [switch]$Undo,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

# Each browser's policy key, the value that sets private windows, and what "off" is.
$Policies = @(
    @{ Browser = 'Chrome';   Key = 'HKLM:\SOFTWARE\Policies\Google\Chrome';        Name = 'IncognitoModeAvailability'; Off = 1 },
    @{ Browser = 'Chromium'; Key = 'HKLM:\SOFTWARE\Policies\Chromium';             Name = 'IncognitoModeAvailability'; Off = 1 },
    @{ Browser = 'Edge';     Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge';       Name = 'InPrivateModeAvailability'; Off = 1 },
    @{ Browser = 'Brave';    Key = 'HKLM:\SOFTWARE\Policies\BraveSoftware\Brave';  Name = 'IncognitoModeAvailability'; Off = 1 },
    @{ Browser = 'Brave';    Key = 'HKLM:\SOFTWARE\Policies\BraveSoftware\Brave';  Name = 'TorDisabled';               Off = 1 }
)

function Get-Value($p) {
    $item = Get-ItemProperty -LiteralPath $p.Key -Name $p.Name -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    return $item.($p.Name)
}

if ($Status) {
    foreach ($p in $Policies) {
        $v = Get-Value $p
        $state = if ($v -eq $p.Off) { 'off' } elseif ($null -eq $v) { 'on' } else { "set to $v" }
        Write-Host ('{0,-9} {1,-26} {2}' -f $p.Browser, $p.Name, $state)
    }
    exit 0
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host 'Run this in an administrator PowerShell: these are machine policies.' -ForegroundColor Red
    exit 1
}

foreach ($p in $Policies) {
    if ($Undo) {
        if ($null -ne (Get-Value $p)) {
            Remove-ItemProperty -LiteralPath $p.Key -Name $p.Name
        }
    } else {
        if (-not (Test-Path -LiteralPath $p.Key)) { New-Item -Path $p.Key -Force | Out-Null }
        New-ItemProperty -LiteralPath $p.Key -Name $p.Name -PropertyType DWord -Value $p.Off -Force | Out-Null
    }
}

if ($Undo) {
    Write-Host 'Private windows are allowed again. Each browser takes it at its next start.'
} else {
    Write-Host 'Private windows are off in Chrome, Chromium, Edge and Brave. Each browser takes it at its next start.'
}
