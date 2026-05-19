@echo off
REM Full stack: trading engine + Streamlit UI (recommended startup)
cd /d "%~dp0"

echo Starting Bot Runner (engine/runner.py)...
start "Bot Runner" python engine\runner.py

echo Waiting for engine startup sync...
timeout /t 5 /nobreak >nul

echo Starting Streamlit UI...
streamlit run ui/app.py
