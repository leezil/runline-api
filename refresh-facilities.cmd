@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh-facilities.ps1" %*
set "refreshExit=%ERRORLEVEL%"
echo.
echo Refresh exit code: %refreshExit% (0 = completed or safely skipped, 1 = failed or partial)
echo Logs: %~dp0.cache\facilities\job-runs
pause
exit /b %refreshExit%
