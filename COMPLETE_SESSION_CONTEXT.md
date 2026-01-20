# MASTER SESSION CONTEXT - All Information Combined

## 🔐 N8N + HuggingFace Configuration

### N8N Instance
- **URL**: https://gionie-n8n-free.hf.space/
- **API Base**: https://gionie-n8n-free.hf.space/api/v1

### Authentication Token (EXPIRED - NEEDS REFRESH)
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiOTY0ZmU0Zi0zZTU3LTQwNjAtOTJlMy1hOTRhMGJlMmI0ZjgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwiaWF0IjoxNzY4ODk1MjU5LCJleHAiOjE3NzE0MzA0MDB9.Qr9LZvt6DZDq8D4R2NlXDCPFAbpJ2CXtQf6-HBYyUdo
```
- **Status**: EXPIRED (got 401 Unauthorized when testing)
- **Expiry**: ~2025-01-17 (from JWT payload)
- **Action Required**: Generate new token from n8n UI

### How to Generate New Token
1. Open https://gionie-n8n-free.hf.space/
2. Go to **Settings** → **API**
3. Click **Generate New API Key**
4. Copy the new token and update this file

---

## 🤖 Crypto Quant Bot Investigation

### Problem Statement
> "5 running bots, but only 2 open orders on exchange"

**Expected behavior**: Each bot should have 1 position + 1 TP order + 1 Next Order (NO)
**Actual behavior**: 5 bots running but exchange shows only 2 orders

### Root Cause Analysis
**First-Claim Policy** - Multi-bot architecture:
- First bot to place entry order becomes "OWNER"
- Other bots on same trading pair become "PASSENGERS" (monitor only)
- Passenger bots don't place orders on exchange - they track owner's position
- This explains 5 bots but only 2 orders (1 owner + 4 passengers, or similar ratio)

### Key Files
| File | Purpose |
|------|---------|
| `engine/database.py:575-705` | Order tracking (`save_bot_order`, `get_bot_order_ids`) |
| `engine/runner.py:343-412` | Order filtering by bot |
| `engine/reconciliation.py` | Position ownership determination |
| `docs/SYSTEM_ARCHITECTURE.md` | Architecture documentation |

### Database Schema
```sql
-- trades table (order tracking):
- entry_order_id TEXT
- tp_order_id TEXT
- bot_position_id TEXT

-- bot_orders table (detailed tracking):
- bot_id, step, order_type, order_id, price, amount, status
```

### Quick Diagnostic Commands (Memory-Safe)
```bash
# Count running bots
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM bots WHERE is_active=1"

# Count bots with entry orders
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM trades WHERE entry_order_id IS NOT NULL"

# Count bots with TP orders
sqlite3 crypto_bot.db "SELECT COUNT(*) FROM trades WHERE tp_order_id IS NOT NULL"

# Show bots with their order IDs
sqlite3 crypto_bot.db "SELECT bot_id, entry_order_id, tp_order_id FROM trades"
```

### Common Causes (Ranked by Likelihood)
1. **Order ID Not Saved**: `save_bot_order()` not called in some code paths
2. **Passenger Bot Syndrome**: Expected - passengers don't place orders
3. **Orphaned Orders**: Orders from previous sessions not cleaned up
4. **Race Condition**: Orders placed but not saved to DB before crash

---

## 📁 Files Created for Persistence
- `COMPLETE_SESSION_CONTEXT.md` - This file (master copy)
- `CRASH_INVESTIGATION_CONTEXT.md` - Detailed crash context
- `QUICK_INVESTIGATION_CHECKLIST.md` - Fast recovery steps
- `N8N_HUGGINGFACE_CONFIG.md` - N8N-specific config
- `load_context.py` - Auto-load script (run this after any crash)
- `load_investigation_context.bat` - Windows batch alternative

---

## 🔄 Workflow After Any Crash
1. Run: `python load_context.py`
2. Read the output
3. Continue investigation from where you left off
4. Update this file with new findings

---

## ⚠️ Memory Safety Tips
- Use `grep` instead of `read` for large files
- Run one agent at a time (avoid parallel)
- Use SQLite CLI commands (memory-safe) vs Python scripts
- If OOM again: just save context and restart

---

**Last Updated**: 2026-01-20
**Location**: Crypto_Quant_Bot\COMPLETE_SESSION_CONTEXT.md
