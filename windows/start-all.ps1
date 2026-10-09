# Starts the xrnmoni server + agent in the background (no visible windows).
# Registered by install.ps1 to run at logon. You can also double-click
# start-xrnmoni.bat to run it manually.

$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot
$root = Split-Path $here -Parent

$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"   # pythonw = no console window
$token = (Get-Content (Join-Path $here "token.txt") -Raw).Trim()
$env:XRNMONI_TOKEN = $token

# Server (dashboard + storage)
Start-Process -FilePath $pyw `
    -ArgumentList @("-m","uvicorn","main:app","--host","0.0.0.0","--port","8000") `
    -WorkingDirectory (Join-Path $root "server") -WindowStyle Hidden

Start-Sleep -Seconds 2

# Agent (tracks apps + idle, and receives browser events)
Start-Process -FilePath $pyw `
    -ArgumentList @("agent.py","--config",(Join-Path $root "agent\config.toml")) `
    -WorkingDirectory (Join-Path $root "agent") -WindowStyle Hidden
