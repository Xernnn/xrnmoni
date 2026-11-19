@echo off
REM Double-click to start xrnmoni now (it also starts by itself at every logon).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
timeout /t 5 >nul
