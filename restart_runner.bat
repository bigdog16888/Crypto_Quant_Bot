@echo off
echo Stopping bot runner...
taskkill /F /FI "WINDOWTITLE eq Bot Runner*" 2>nul
taskkill /F /FI "IMAGENAME eq python.exe" /FI "MEMUSAGE gt 50000" 2>nul
timeout /t 2 /nobreak >nul

echo Cleaning up stale stop/emergency signals...
del engine.stop 2>nul
del engine.emergency 2>nul

echo Starting bot runner with fix...
cd /d "%~dp0"
start "Bot Runner" python engine\runner.py
echo Bot runner restarted. Check engine_runner_debug.log for output.
timeout /t 5 /nobreak

echo Running deployment verification check...
python scripts\verify_deployment.py
if %errorlevel% neq 0 (
    echo 🛑 [ERROR] Deployment verification failed!
    exit /b %errorlevel%
)
echo ✅ [SUCCESS] Deployment fully verified and live.
