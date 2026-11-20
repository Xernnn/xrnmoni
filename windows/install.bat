@echo off
REM Double-click to install xrnmoni for the user signed in now (no administrator needed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
