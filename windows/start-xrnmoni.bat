@echo off
REM Double-click to start xrnmoni manually (normally it auto-starts at logon).
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
echo xrnmoni started.
timeout /t 3 >nul
