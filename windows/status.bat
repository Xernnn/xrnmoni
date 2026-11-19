@echo off
REM Double-click to see whether xrnmoni is running and reaching the hub.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1"
pause
