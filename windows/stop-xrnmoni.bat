@echo off
REM Double-click to stop xrnmoni until start-xrnmoni.bat starts it again.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
timeout /t 5 >nul
