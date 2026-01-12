@echo off
cd /d "%~dp0"
echo Starting Crypto Quant Bot...
streamlit run ui/app.py
pause