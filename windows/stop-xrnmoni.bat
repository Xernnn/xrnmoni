@echo off
REM Double-click to stop xrnmoni (both server and tracker).
taskkill /f /im pythonw.exe >nul 2>&1
echo xrnmoni stopped.
timeout /t 2 >nul
