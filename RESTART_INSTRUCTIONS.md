# Restart Instructions

## Before Restart
✅ All context files saved:
- `CRASH_INVESTIGATION_CONTEXT.md` - Full investigation context
- `QUICK_INVESTIGATION_CHECKLIST.md` - Quick checklist
- `load_context.py` - Python loader script
- `INVESTIGATION_SESSION_STATE.json` - Machine-readable state

## After Restart
1. Open terminal in: `C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot`

2. Run the context loader:
   ```bash
   python load_context.py
   ```

3. This will:
   - Print full investigation context
   - Run quick database diagnostics
   - Show current bot/order state

4. Continue from "Next Steps" in `CRASH_INVESTIGATION_CONTEXT.md`

## Quick Diagnostic (No Python Needed)
```bash
# Count running bots
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM bots WHERE is_active=1"

# Count bots with orders tracked
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM trades WHERE entry_order_id IS NOT NULL"

# Count open orders in bot_orders table
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM bot_orders WHERE status='open'"
```

## If Memory Issues Again
- Use `sqlite3` CLI commands instead of Python scripts
- Use `grep` instead of `read` for file searching
- Run one command at a time
- Don't use parallel agents
