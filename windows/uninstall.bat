@echo off
REM Double-click to remove xrnmoni for this user. Settings, logs and unsent events stay
REM unless you run: powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData
REM (One line, so cmd doesn't read on from a file the uninstaller may have deleted.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %* & pause & exit /b
