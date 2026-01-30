# =========================================
# CRYPTO QUANT BOT - EMERGENCY HANDOFF & RECOVERY GUIDE
# =========================================
# Generated: 2026-01-30 17:00 UTC
# Session: SES_3f3110326ffe2svX8tkl2yplpc
# =========================================

## 🚨 CRITICAL: Current Bot State

### Bots Status
| Bot ID | Name | Pair | Status | Invested | Step | Issue |
|--------|------|------|--------|----------|------|-------|
| 41 | btc long | BTC/USDC | IDLE | $0.00 | 0 | Position cleared by bug |
| 43 | long btc price | BTC/USDC | IDLE | $0.00 | 0 | Position cleared by bug |
| 44 | gold long | XAU/USDT:USDT | IN TRADE | $10.00 | 0 | Active position |

### Known Issues
1. **Position Reset Bug**: Bots 41 & 43 had ~$3,038 positions (0.037 BTC) that were incorrectly cleared
2. **Step Calculation Bug**: `import_position_from_exchange()` always set step=1 instead of calculating from position size
3. **Premature Reset**: `reset_bot_after_tp()` cleared positions without verifying exchange state

---

## ✅ FIXES APPLIED

### Fix 1: `reset_bot_after_tp()` - Safety Verification
**File:** `engine/database.py:401-546`

**New Behavior:**
- BEFORE: Always cleared DB when called
- AFTER: Checks exchange positions first, REFUSES to reset if position exists

**Code Pattern:**
```python
def reset_bot_after_tp(bot_id, exit_price=0.0, action_label='TP_HIT', 
                      exchange_positions=None, verify_with_exchange=True):
    if verify_with_exchange and exchange_positions is not None:
        ex_pos = exchange_positions.get(exchange_pair)
        if ex_pos and abs(ex_pos.get('size', 0)) > 0:
            # SAFETY BLOCK - DO NOT RESET!
            logger.critical(f"🚨 SAFETY BLOCK: Exchange still has position!")
            return  # Refuse to reset
```

**Impact:** Prevents position loss during engine restarts and reconciliation timing issues.

---

### Fix 2: `import_position_from_exchange()` - Correct Step Calculation
**File:** `engine/database.py:562-627`

**New Behavior:**
- BEFORE: Always set step=1 regardless of position size
- AFTER: Calculates correct step from position size: `step = log(size/base) / log(multiplier)`

**Code Pattern:**
```python
def import_position_from_exchange(bot_id, pair, position_size, entry_price, direction):
    # Get bot's base_size and multiplier
    cursor.execute('SELECT base_size, martingale_multiplier FROM bots WHERE id = ?', (bot_id,))
    base_size, multiplier = cursor.fetchone()
    
    # Calculate correct step from position size
    total_invested = position_size * entry_price
    calculated_step = calculate_step_from_position(total_invested, base_size, multiplier)
    
    # Log variance warning if position doesn't match expected pattern
    expected_at_step = base_size * (multiplier ** calculated_step)
    size_variance = abs(total_invested - expected_at_step) / expected_at_step
    if size_variance > 0.1:
        logger.warning(f"⚠️ Position variance: {size_variance*100:.1f}%")
    
    # Update with CORRECT step
    cursor.execute('''UPDATE trades SET current_step = ? WHERE bot_id = ?''', 
                  (calculated_step, bot_id))
```

**Impact:** Imported positions now show correct martingale step (e.g., step 5 for $3,038 position on 1.8x multiplier).

---

### Fix 3: New Helper Function - `calculate_step_from_position()`
**File:** `engine/database.py:374-398`

```python
def calculate_step_from_position(position_size: float, base_size: float, multiplier: float) -> int:
    """
    Calculate martingale step from position size.
    Uses: position_size = base_size * (multiplier ^ step)
    Solving: step = log(position_size / base_size) / log(multiplier)
    """
    if position_size <= 0 or base_size <= 0 or multiplier <= 1:
        return 0
    
    ratio = position_size / base_size
    if ratio <= 1:
        return 0
    
    import math
    step = math.log(ratio) / math.log(multiplier)
    return max(0, round(step))
```

---

## 📊 ROOT CAUSE ANALYSIS

### What Happened to Bots 41 & 43:

```
Timeline:
┌─────────────────────────────────────────────────────────────────────┐
│ Jan 30 15:56 - Position Import                                       │
│   - Exchange had 0.037 BTC @ $82,113 (~$3,038)                      │
│   - import_position_from_exchange() called                           │
│   - ❌ WRONG: Set step=1 (should be step 5!)                         │
│   - Trade history logged: "POSITION_IMPORT" with step=1              │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Jan 30 15:56:11 - Reconciliation Run                                 │
│   - StateReconciler fetched exchange positions                       │
│   - Due to API timing, positions temporarily not returned            │
│   - Bot marked as "IN_TRADE but no exchange position"               │
│   - has_confirmed_entry() checked trade_history                      │
│   - ❌ BUG: Only looked for BUY/SELL, ignored POSITION_IMPORT        │
│   - Result: has_confirmed_entry = False                              │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Jan 30 15:56:11 - Reset Triggered                                    │
│   - Scenario 1 triggered: "Ghost Trade" (DB=IN_TRADE, EX=NONE)      │
│   - reset_bot_after_tp() called with action_label="OFFLINE_CLOSE"   │
│   - ❌ WRONG: Cleared DB without verifying exchange!                 │
│   - UPDATE trades SET total_invested=0, current_step=0...            │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ RESULT:                                                              │
│   - DB: total_invested=0, step=0 (looks like IDLE)                  │
│   - EXCHANGE: Still has 0.037 BTC position!                          │
│   - Bot appears "clean" but position exists on exchange             │
│   - NO PnL tracking, NO proper management                           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🔧 RECOVERY STEPS

### For Bots 41 & 43 (Position Missing from DB):

```bash
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot

# Option 1: Run reconciliation to re-import
python engine/runner.py

# Option 2: Manual verification
python -c "
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

# Check exchange
ex = ExchangeInterface()
positions = ex.exchange.fetch_positions()
print('Exchange positions:', positions)
ex.close()

# If position exists but DB is empty, need manual fix
"
```

### To Start the Bot:
```bash
cd C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot
python engine/runner.py
```

---

## 🏠 HOME WORKSTATION SETUP

### Quick Start Checklist:

```bash
# 1. Clone repository
git clone https://github.com/YOUR_USERNAME/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot

# 2. Copy environment template
cp .env.example .env
# Edit .env and add your API keys

# 3. Install dependencies
pip install -r requirements.txt

# 4. Verify setup
python -c "from engine.database import init_db; print('OK')"

# 5. Start the engine
python engine/runner.py
```

### Required API Keys:
1. **Binance Testnet API Keys**
   - URL: https://testnet.binancefuture.com/
   - Get API Key and Secret
   - Add to `.env` as `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`

2. **Verify `.env` is NOT committed**
   - Check: `git status` should NOT show `.env`
   - If it does: `git rm --cached .env` then `git add .gitignore`

---

## 💾 DATABASE BACKUP

### Automatic Backups:
- Location: `backups/` folder
- Naming: `crypto_bot_YYYYMMDD_HHMMSS.db`
- Retention: Keep last 10 backups

### Manual Backup:
```bash
# Create backup
python tools/backup_db.py

# Restore from backup
cp backups/crypto_bot_20260130_120000.db crypto_bot.db
```

### Backup Script Location:
`tools/backup_db.py` - Run this before any major changes!

---

## 📁 KEY FILES MODIFIED

| File | Change | Impact |
|------|--------|--------|
| `engine/database.py` | Fixed `reset_bot_after_tp()` | Safety verification before reset |
| `engine/database.py` | Fixed `import_position_from_exchange()` | Correct step calculation |
| `engine/database.py` | Added `calculate_step_from_position()` | Helper for step calculation |
| `.env.example` | Updated template | Clearer setup instructions |
| `tools/backup_db.py` | Created | Database backup system |

---

## 🎯 TESTING PLAN

### Before Forward Testing:
1. ✅ Verify `.env` is configured with correct API keys
2. ✅ Run `python engine/runner.py`
3. ✅ Check logs for `🚨 SAFETY BLOCK` messages (should be none if positions closed)
4. ✅ Verify bot states in UI match expected

### What to Watch:
- `engine.log` - Look for ERROR, CRITICAL
- Position sizes - Should match calculated expectations
- Step progression - Should increment correctly on martingale

---

## 📞 QUICK REFERENCE

| Command | Purpose |
|---------|---------|
| `python engine/runner.py` | Start the trading engine |
| `python tools/backup_db.py` | Create database backup |
| `python tools/force_reset.py` | Manually reset bot states |
| `streamlit run ui/app.py` | Start the monitoring UI |

---

## 🔐 SECURITY NOTES

- ✅ `.env` is in `.gitignore` (never committed)
- ✅ API keys stored in `.env` only
- ⚠️ `.env.example` contains TEMPLATE values only
- ⚠️ When working from home, copy `.env.example` to `.env` and add real keys

---

## 📝 SESSION NOTES

**Session ID:** SES_3f3110326ffe2svX8tkl2yplpc

**Key Findings:**
- Position size mismatch: Exchange showed 0.037 BTC (~$3,038) but DB showed $0
- This is 16x-17x the base size, meaning step 5-6 on 1.8x multiplier
- The old code incorrectly set step=1 on all imports
- The reset logic didn't verify exchange state before clearing

**Pending Actions:**
1. [ ] Verify exchange has no orphaned positions
2. [ ] Start engine and confirm stable operation
3. [ ] Monitor for any further position sync issues

---

*Generated by Sisyphus AI Agent - 2026-01-30*
