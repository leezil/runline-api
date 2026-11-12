@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup-facilities.ps1"
set "backupExit=%ERRORLEVEL%"
echo.
echo Backup exit code: %backupExit%
echo Backups: %~dp0.cache\facilities\backups
pause
exit /b %backupExit%
