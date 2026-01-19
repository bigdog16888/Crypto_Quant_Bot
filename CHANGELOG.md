# Changelog - Crypto Quant Bot

## Changes Made (2026-01-19)

### 🎯 ATR Timeframe Fix
**File**: `ui/views/monitor.py` (lines 448-467)

**Problem**: ATR values for 3d and 5d timeframes were identical to 1d because the exchange doesn't directly support these timeframes.

**Solution**: Calculate using square root scaling:
- 3d ATR = 1d ATR × √3 (1.732)
- 5d ATR = 1d ATR × √5 (2.236)

```python
# Before: All timeframes showed same value
# After: Different values based on period scaling
atr_data['3d'] = {'atr': atr_1d * 1.732, ...}
atr_data['5d'] = {'atr': atr_1d * 2.236, ...}
```

### 🔄 P/L Sync Improvements
**File**: `ui/views/monitor.py` (lines 518-554)

**Problem**: 
- Bots show "In Trade" with P/L
- But "Open Positions (Exchange)" shows empty

**Solution**: Added early exchange position fetching to create unified view:

```python
# Fetch exchange positions BEFORE processing bots
exchange_positions = {}
try:
    ex_futures = ExchangeInterface(market_type='future')
    fut_positions = ex_futures.exchange.fetch_positions()
    for pos in fut_positions:
        # Store actual exchange positions
        exchange_positions[sym] = {...}
except Exception as e:
    st.warning(f"Could not fetch futures positions: {e}")
```

### 🔄 Multi-Bot Order ID Tracking (v0.4.1)
**Files**: 
- `engine/database.py` - New order tracking functions
- `engine/runner.py` - Save order IDs on placement
- `ui/views/monitor.py` - Show per-bot order breakdown

**Problem**: 
```
Bot A: LONG BTC/USDC @ 94000 TP
Bot B: LONG BTC/USDC @ 94000 TP
Exchange: Shows "1 TP order @ 94000" (combined)

Bot A cancels "its" TP → Cancels THE TP order (affects Bot B too!)
```

**Solution**: Track order IDs per bot:

```python
# 1. Database stores order IDs per bot
# New columns: entry_order_id, tp_order_id
# New table: bot_orders (for grid orders)

# 2. When placing order, save ID
save_bot_order(bot_id, 'entry', order_id, price, amount)
save_bot_order(bot_id, 'tp', order_id, price, amount)

# 3. Match exchange orders to bots by ID
def get_bots_by_order_id(order_id):
    # Checks trades table and bot_orders table
    # Returns which bot(s) own this order

# 4. Display shows per-bot breakdown
# Exchange: 1 TP @ 94000
# UI shows: Bot A (tp) | Bot B (tp) | Manual
```

**Benefits:**
- Each bot manages its own orders
- Cancel/Modify only affects that bot's orders
- Clear visibility: "Bot A has this order, Bot B has that order"
- Manual orders marked separately

### 📊 Streamlit API Fixes
**File**: `ui/views/monitor.py` (lines 11, 187-190, 692-698)

**Problem**: 
- `st.column_global_config` doesn't exist in Streamlit
- `global_config.settings` import failed

**Solution**:
```python
# Before
from global_config.settings import config as global_config
st.column_global_config.NumberColumn(...)

# After  
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import config as global_config
st.column_config.NumberColumn(...)
```

### 🧪 Playwright Tests
**File**: `tests/test_pl_sync.py` (NEW)

Comprehensive test suite covering:
- Positions sync with bots
- Open orders display
- Exchange positions match DB
- ATR values differ by timeframe
- Default settings (20x, 1.8, 1.5%, 1.1)
- P/L calculations
- Grid visualizer
- One bot per pair restriction

### 📈 Performance Analysis
**File**: `PERFORMANCE_ANALYSIS.md` (NEW)

Documents:
- Sequential bot processing bottleneck (10x slower with 10 bots)
- P/L sync issue root cause analysis
- Debugging steps
- Quick fixes
- Recommended improvements

---

## Quick Fix for P/L Sync Issue

If your bots show "In Trade" but exchange shows no positions:

```bash
# Option 1: Restart runner (triggers sync on startup)
cd D:\Crypto_Quant_Bot
python -m engine.runner

# Option 2: Run cleanup script
python cleanup_ghost_trades.py

# Option 3: Manual DB check
python verify_db_connection.py
```

---

## Files Modified

```
Modified:
├── ui/views/monitor.py        # ATR fix, P/L sync, API fixes
├── ui/views/bot_creator.py    # One bot per pair restriction
├── README.md                  # Updated documentation

Created:
├── tests/test_pl_sync.py      # Playwright tests
├── PERFORMANCE_ANALYSIS.md    # Architecture analysis
└── CHANGELOG.md               # This file
```

---

## Known Issues

1. **LSP Type Errors**: Some type errors in IDE (not runtime errors)
   - `st.column_config` vs `st.column_global_config`
   - Exchange interface method returns
   - These are type checking issues, not functional bugs

2. **Performance**: Sequential bot processing
   - Documented in PERFORMANCE_ANALYSIS.md
   - Parallel processing solution provided

3. **Exchange API**: Some timeframes may not be supported
   - 3d, 5d calculated via scaling
   - Error handling in place

4. **Multiple Bots on Same Pair**: Now BLOCKED
   - Use different pairs or edit existing bot instead

---

## Testing Checklist

- [ ] ATR shows different values for 4h, 1d, 3d, 5d
- [ ] Running bots show P/L correctly
- [ ] Open Positions (Exchange) matches bot state
- [ ] Sync status indicator shows SYNCED
- [ ] Default settings: 20x leverage, 1.8 martingale, 1.5% TP, 1.1 ATR grid
- [ ] Early Exit settings reflected in chart
- [ ] Open Orders show which bot owns each order
- [ ] Manual orders marked as "MANUAL"
- [ ] Multiple bots on same pair show individual order IDs
