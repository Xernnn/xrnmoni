# xrnmoni - Windows 11 one-shot installer (run on the monitored PC).
#
# What it does:
#   1. Installs Python if it's missing (via winget).
#   2. Creates a private Python environment and installs the agent + server.
#   3. Writes the config with a token you choose (or a random one).
#   4. Opens the dashboard port ONLY to Tailscale (not the open internet).
#   5. Registers everything to start automatically when this PC logs on.
#
# HOW TO RUN (one time):
#   - Right-click the Start button -> "Terminal (Admin)" / "PowerShell (Admin)".
#   - Then run:
#       powershell -ExecutionPolicy Bypass -File "C:\path\to\xrnmoni\windows\install.ps1"
#
# It is safe to run again; it just re-applies the setup.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent        # the xrnmoni project folder
Write-Host "xrnmoni installer - project at: $root" -ForegroundColor Cyan

# --- 1. Python ---------------------------------------------------------------
function Have-Python { try { & py -3 --version *> $null; return $true } catch { return $false } }
if (-not (Have-Python)) {
    Write-Host "Python not found. Installing via winget..." -ForegroundColor Yellow
    try {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    } catch {
        Write-Host "Could not auto-install Python." -ForegroundColor Red
        Write-Host "Please install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this." -ForegroundColor Red
        exit 1
    }
}
Write-Host "Python OK: $(& py -3 --version)" -ForegroundColor Green

# --- 2. Virtual environment + dependencies -----------------------------------
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) { & py -3 -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r (Join-Path $root "server\requirements.txt")
& $py -m pip install -r (Join-Path $root "agent\requirements.txt")
Write-Host "Dependencies installed." -ForegroundColor Green

# --- 3. Token + config -------------------------------------------------------
$tokenFile = Join-Path $PSScriptRoot "token.txt"
if (Test-Path $tokenFile) {
    $token = (Get-Content $tokenFile -Raw).Trim()
    Write-Host "Reusing existing token from token.txt."
} else {
    $entered = Read-Host "Choose a password/token for the dashboard (press Enter to auto-generate)"
    if ([string]::IsNullOrWhiteSpace($entered)) { $token = [guid]::NewGuid().ToString("N") }
    else { $token = $entered.Trim() }
    Set-Content -Path $tokenFile -Value $token -NoNewline
}

$device = $env:COMPUTERNAME
$config = @"
server_url = "http://127.0.0.1:8000"
token = "$token"
device = "$device"
poll_interval = 2.0
flush_interval = 10.0
afk_threshold = 60.0
bridge_port = 8129
"@
Set-Content -Path (Join-Path $root "agent\config.toml") -Value $config
Write-Host "Config written (device name: $device)." -ForegroundColor Green

# --- 4. Firewall: allow the dashboard port ONLY from Tailscale ---------------
# 100.64.0.0/10 is Tailscale's private address range. This means the dashboard
# is NOT reachable from the open internet or random devices - only your linked
# Tailscale devices.
try {
    Get-NetFirewallRule -DisplayName "xrnmoni dashboard" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
} catch {}
New-NetFirewallRule -DisplayName "xrnmoni dashboard" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 8000 -RemoteAddress 100.64.0.0/10 | Out-Null
Write-Host "Firewall rule added (port 8000, Tailscale only)." -ForegroundColor Green

# --- 5. Auto-start at logon --------------------------------------------------
$startScript = Join-Path $PSScriptRoot "start-all.ps1"
$taskCmd = "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`""
schtasks /create /tn "xrnmoni" /tr $taskCmd /sc onlogon /rl highest /f | Out-Null
Write-Host "Registered to start automatically at logon." -ForegroundColor Green

# --- Done: start it now and show how to reach it -----------------------------
Write-Host "`nStarting xrnmoni now..." -ForegroundColor Cyan
& powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $startScript

Write-Host "`n==================== DONE ====================" -ForegroundColor Green
$tsIp = $null
try { $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1) } catch {}
if ($tsIp) {
    Write-Host "Open this on your phone/laptop (with Tailscale on):" -ForegroundColor Cyan
    Write-Host "    http://$tsIp:8000" -ForegroundColor White
} else {
    Write-Host "Next: install Tailscale on THIS pc and on your phone/laptop (same login)." -ForegroundColor Yellow
    Write-Host "Then open  http://<this-pc-tailscale-ip>:8000  from your device." -ForegroundColor Yellow
}
Write-Host "Your dashboard token/password is:" -ForegroundColor Cyan
Write-Host "    $token" -ForegroundColor White
Write-Host "=============================================`n" -ForegroundColor Green
