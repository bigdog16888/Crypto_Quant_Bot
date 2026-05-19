@echo off
REM UI only. The trading engine does NOT start until you click
REM   "Start Monitoring" in the sidebar, OR you use run_stack.bat
cd /d "%~dp0"
echo Starting Crypto Quant Bot UI...
echo   - For engine + UI together: run run_stack.bat
echo   - Or click "Start Monitoring" in the sidebar after this opens
streamlit run ui/app.py
pause