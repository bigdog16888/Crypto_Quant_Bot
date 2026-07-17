@echo off
REM CQB session-start dated-reminder redundant net (Windows Task Scheduler).
REM Runs scripts/session_start_check.py daily independent of the Hermes process,
REM so dated reminders (hy3 07-21, laguna 07-28) surface even if the laptop was
REM off at the cron time. Mirrors the session-start check; authoritative net is
REM the script itself (scripts/session_start_check.py).
setlocal
set PY=C:\Users\Gionie\AppData\Local\Programs\Python\Python310\python.exe
set REPO=C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot
set PATH=C:\Users\Gionie\AppData\Local\Programs\Python\Python310;C:\Users\Gionie\AppData\Local\Programs\Python\Python310\Scripts;%PATH%
set PYTHONPATH=
set LOG=%REPO%\session_start_task.log
echo [%DATE% %TIME%] Task Scheduler run >> "%LOG%"
"%PY%" "%REPO%\scripts\session_start_check.py" >> "%LOG%" 2>&1
echo [%DATE% %TIME%] exit=%ERRORLEVEL% >> "%LOG%"
endlocal
