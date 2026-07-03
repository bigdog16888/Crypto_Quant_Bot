@echo off
REM Create a version-tagged backup (code + crypto_bot.db) in backups\
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\create_version_backup.ps1" %*
if errorlevel 1 (
    echo Backup failed.
    pause
    exit /b 1
)
echo.
pause
