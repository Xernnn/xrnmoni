@echo off
REM Removes the auto-start task and firewall rule, and stops xrnmoni.
REM (Leaves the project folder and saved data in place.)
taskkill /f /im pythonw.exe >nul 2>&1
schtasks /delete /tn "xrnmoni" /f >nul 2>&1
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'xrnmoni dashboard' -ErrorAction SilentlyContinue"
echo xrnmoni removed from startup. Data and files are still on disk.
pause
