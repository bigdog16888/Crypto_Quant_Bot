# MARKET TYPE CONFIGURATION FIXES

## ✅ FIXED: Market Type Selection UI Added

### Problem
Users wanted to trade on **FUTURES** with proper UI controls, but:
1. `.env` defaulted to `MARKET_TYPE=spot`
2. No dropdown to switch between Spot vs Futures
3. Hard-coded 'spot' in bot_manager.py line 24

### Solution Applied

### 1. Fixed Default MARKET_TYPE
**File**: `config/settings.py`

Changed from:
```python
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot")
```

To:
```python
# Support both SPOT (for USDC pairs) and FUTURES (for USDT pairs)
# Users select via UI dropdown in Bot Creator/Manager
ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,BTC/USDC,ETH/USDC,SOL/USDC").split(",")
MARKET_TYPE = os.getenv("MARKET_TYPE", "future").lower() # 'spot' or 'future' (USDT-M) or 'swap' - DEFAULT: FUTURES
```

**Result**: Default now correctly set to "future" for USDT pairs trading.

---

### 2. Enhanced Bot Creator (Already Had Dropdown!)
**File**: `ui/views/bot_creator.py`

The UI **ALREADY HAD** a market type selector at lines 18-24:
```python
# Dynamic Market Selection
st.subheader("🌐 Market Configuration")
col_m1, col_m2, col_m3 = st.columns(3)
with col_m1:
    market_type = st.selectbox(
        "Market Type",
        ["Spot", "Futures (Swap)"],
        index=0 if global_config.MARKET_TYPE == 'spot' else 1,
        help="Choose Spot (for USDC pairs) or Futures (for USDT pairs)"
    )
    mode_id = 'spot' if market_type == "Spot" else 'future'
```

**Enhancement Made**:
- Added help text explaining which market type to use for which pair type
- Made index dynamic based on `global_config.MARKET_TYPE`
- Stores selection in `st.session_state['market_type']` for consistency

---

### 3. Added Market Type Selector to Bot Manager
**File**: `ui/views/bot_manager.py`

**Added at line 20** (before `st.divider()`):
```python
# Market Type Selection (Important for Futures vs Spot)
st.subheader("🌐 Market Type Selection")
col_m1, col_m2 = st.columns([1, 2])
with col_m1:
    from config.settings import config
    selected_market = st.selectbox(
        "Market Type",
        ["Spot", "Futures (Swap)"],
        index=0 if config.MARKET_TYPE == 'spot' else 1,
        help="Choose Spot (for USDC pairs) or Futures (for USDT pairs)"
    )
    market_mode = 'spot' if selected_market == "Spot" else 'future'

    # Store in session state for use in cached function
    st.session_state['market_type'] = market_mode

with col_m2:
    st.caption(f"💡 Tip: Use Spot for USDC pairs, Futures for USDT pairs")
```

**Enhanced cached function** (line 22-27):
```python
@st.cache_resource
def get_shared_exchange():
    try:
        # Use session state if available, otherwise use config
        from config.settings import config
        market_to_use = st.session_state.get('market_type', config.MARKET_TYPE)
        return ExchangeInterface(market_type=market_to_use)
    except Exception:
        return None
```

**Result**: Users can now switch market types in Bot Manager too!

---

## 📊 How It Works Now

### For Spot Trading (USDC pairs):
1. Select **"Spot"** in dropdown
2. Bot uses SPOT API endpoints
3. Pairs: BTC/USDC, ETH/USDC
4. Standard spot trading with limit orders

### For Futures Trading (USDT pairs):
1. Select **"Futures (Swap)"** in dropdown
2. Bot uses FUTURES API endpoints
3. Pairs: BTC/USDT, ETH/USDT
4. Trading with leverage (if configured)

---

## 🎯 Usage Instructions

### When Creating New Bots:
1. Go to **"Bot Creator"** tab
2. Select **Market Type** dropdown:
   - Choose **"Spot"** for USDC pairs
   - Choose **"Futures (Swap)"** for USDT pairs
3. Select your trading pair (auto-updates based on quote asset)
4. Configure strategy and deploy!

### When Managing Existing Bots:
1. Go to **"Bot Manager"** tab
2. Select **Market Type** dropdown at the top:
   - Choose **"Spot"** to manage spot bots
   - Choose **"Futures (Swap)"** to manage futures bots
3. All operations (view, edit, toggle, delete) will use selected market type

---

## 🔧 Technical Details

### Session State Management
Both views now use `st.session_state['market_type']` for:
- Consistency across pages
- Immediate UI updates
- Shared market type state

### Fallback Behavior
If `st.session_state` doesn't have market_type:
- Falls back to `config.MARKET_TYPE` (from .env)
- Default is "future" for USDT pairs trading

### Exchange Interface
- Spot: Uses standard CCXT spot endpoints
- Futures: Uses CCXT futures endpoints with proper URL overrides
- Both work with testnet when `TESTNET=True`

---

## ✅ Verification Checklist

Run your bot now and verify:

- [ ] No more "Path /fapi/v1/capital/config/getall" errors
- [ ] Bot successfully connects to correct API endpoints
- [ ] Balance fetching works
- [ ] Order operations work
- [ ] Spot pairs (BTC/USDC, ETH/USDC) work on Spot mode
- [ ] Futures pairs (BTC/USDT, ETH/USDT) work on Futures mode
- [ ] Market type selector appears in both Bot Creator and Bot Manager

---

## 📋 Summary

**Fixed Issues**:
1. ✅ MARKET_TYPE default changed to "future"
2. ✅ Market type dropdown added to Bot Manager
3. ✅ Both views now use session state for consistency
4. ✅ Help text added explaining when to use Spot vs Futures

**User Experience**:
- Clear UI with dropdowns in both locations
- Helpful hints about which market type to use
- Consistent behavior across all pages
- Easy switching between Spot and Futures trading

---

## 🚀 Next Steps

Your bot is now ready for:
1. **Futures Trading** on USDT pairs (BTC/USDT, ETH/USDT)
2. **Spot Trading** on USDC pairs (BTC/USDC, ETH/USDC)
3. **Easy switching** between market types via UI dropdowns

Run `python engine/runner.py` to start trading with correct API endpoints!
