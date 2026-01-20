@echo off
REM Crypto Bot Investigation - Auto Context Loader
REM Run this at the start of any session to restore context

echo ============================================
echo CRYPTO BOT INVESTIGATION - SESSION RESTORE
echo ============================================

REM Check if context file exists
if exist "CRASH_INVESTIGATION_CONTEXT.md" (
    echo [OK] Found context file
    type CRASH_INVESTIGATION_CONTEXT.md
) else (
    echo [WARN] No context file found - fresh session
)

REM Check if quick checklist exists
if exist "QUICK_INVESTIGATION_CHECKLIST.md" (
    echo.
    echo [OK] Found quick checklist
    type QUICK_INVESTIGATION_CHECKLIST.md
)

REM Run quick diagnostic (uses less memory than Python scripts)
echo.
echo ============================================
echo QUICK DATABASE DIAGNOSTIC
echo ============================================
sqlite3 crypto_bot.db "SELECT 'Running Bots: ' || COUNT(*) FROM bots WHERE is_active=1" 2>nul
sqlite3 crypto_bot.db "SELECT 'Bots with Invested > 0: ' || COUNT(*) FROM trades WHERE total_invested > 0" 2>nul
sqlite3 crypto_bot.db "SELECT 'Bots with entry_order_id: ' || COUNT(*) FROM trades WHERE entry_order_id IS NOT NULL" 2>nul
sqlite3 crypto_bot.db "SELECT 'Bots with tp_order_id: ' || COUNT(*) FROM trades WHERE tp_order_id IS NOT NULL" 2>nul
sqlite3 crypto_bot.db "SELECT 'Open orders in bot_orders: ' || COUNT(*) FROM bot_orders WHERE status='open'" 2>nul

echo.
echo ============================================
echo NEXT STEPS
echo ============================================
echo 1. Read CRASH_INVESTIGATION_CONTEXT.md for full context
echo 2. Run QUICK_INVESTIGATION_CHECKLIST.md checks
echo 3. Use sqlite3 commands above for state verification
echo 4. If crash occurs, update CRASH_INVESTIGATION_CONTEXT.md before restarting
echo.
