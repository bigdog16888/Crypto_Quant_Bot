# Changelog - Crypto Quant Bot

## [1.0.1] - 2026-02-12

### Critical Fixes
-   **Data Corruption Purge (Phantom Trades)**: Identified and resolved a severe database corruption where bots were attempting to manage trades with stale entry prices (~$27k BTC) in a ~$96k market. This discrepancy caused all generated orders to be rejected by the exchange, triggering a perpetual "Ghost Bot" reset loop. Executed a surgical database repair to reset these corrupted trades to IDLE, allowing the bots to resume valid trading at current market prices.
-   **Naked Position Auto-Healing**: Fixed a critical bug where bots with open positions but missing orders were incorrectly flagged as "Ghost Bots" and reset to IDLE. The system now verifies if a position exists on the exchange before resetting.
-   **Virtual Position Integrity**: Verified that the "Ghost" detection logic correctly handles "Net Zero" scenarios (e.g., Long 1 BTC + Short 1 BTC = 0 Net) by prioritizing the presence of bot-specific orders (`CQB_{bot_id}_` prefix) over raw exchange position size.

### Improvements
-   **Exchange Data Parsing**: Confirmed `fetch_positions` logic in `exchange_interface.py` correctly parses `contracts` vs `size` to ensure accurate position tracking across different exchange API formats.
-   **Logging**: Added explicit `[SNAPSHOT-DEBUG]` logs to trace position data flow from API to Bot Runner to Executor.

### Technical Details
-   Modified `verify_state_sync` in `engine/bot_executor.py` to check `positions_snapshot` for active contracts before declaring a Ghost.
-   Updated `fetch_positions` in `engine/exchange_interface.py` to ensure robust parsing of position data.

---

## [1.0.0] - 2026-02-12

### 🛡️ Fundamental Multi-Bot Isolation
This release completes the multi-bot architecture by achieving full operational isolation. Bots now operate in their own virtual sandboxes, preventing cross-bot interference that previously caused order wipedowns and state resets.

### 🚀 Major Improvements
- **Scoped Order Cancellation**: Replaced all global `cancel_all_orders(pair)` calls with bot-specific `cancel_orders_by_bot_id(bot_id, pair)`. This ensures that repricing logic in one bot never cancels the grid or TP orders of another bot on the same pair.
- **Aggregate One-Way Reconciliation**: Refactored the `reconciler.py` to handle shared physical positions. It now sums the virtual positions of all active bots for a pair and compares the aggregate size against the exchange position, eliminating false "SIZE MISMATCH" errors in One-Way mode.
- **WebSocket Event Robustness**: Fixed a critical `KeyError` in `ws_event_handlers.py` by correcting dictionary key access for database results. This prevents the real-time stream from crashing upon order fills.
- **Ghost Fix Loop Elimination**: Resolved an issue where `basket_start_time` was not correctly initialized in the database, causing the self-healing system to incorrectly identify and reset active trades as "ghosts."

### 🐛 Bug Fixes
- **WebSocket Handler**: Corrected dictionary access in `_handle_order_filled` and `_handle_order_canceled`.
- **Database Integrity**: Updated `update_martingale_step` to properly initialize `basket_start_time` on initial trade entry.
- **Exchange Safety**: Increased default `base_size` to $150 to ensure all orders clear the Binance USDC Testnet minimum notional hurdles.

---

## [0.9.2] - 2026-02-09


### 🛡️ Critical Infrastructure & State Recovery
This release focuses on restoring fundamental system integrity after detecting a "Ghost Fleet" scenario (DB showing trades that didn't exist on exchange) and unblocking trading operations.

### 🐛 Critical Bug Fixes
- **Ghost Fleet Exorcism**: Detected and repaired 11 bots stuck in "IN TRADE" state with no actual exchange positions. Created `emergency_reset_db.py` to force-align DB with reality (Clean Slate).
- **The "Invisible Wall" (Margin Mode)**: Resolved persistent "Insufficient Funds" errors despite ample wallet balance.
  - **Cause**: Pairs were stuck in **Isolated Margin** mode with 0 allocated funds.
  - **Fix**: Forced `BTC/USDC` and `XAU/USDT` to **CROSS MARGIN** mode via API, unlocking the main wallet for trading.
- **State Synchronization**: Implemented `StateManager` (Singleton) as the Single Source of Truth to prevent future desynchronization between DB, Memory, and Exchange.

### ✨ New Features
- **MTF Trend Confluence**: Added UI toggle for Multi-Timeframe Trend filtering (Bot Creator & Manager).
- **Dynamic Leverage Verification**: Enhanced `BotExecutor` to automatically verify and enforce 20x leverage on startup (confirmed working without hardcoding).

### 🛠️ Technical Improvements
- **Self-Healing**: Unblocked built-in self-healing mechanisms by fixing symbol normalization issues (`BTC/USDC` vs `BTC/USDC:USDC`).
- **Playwright Verification**: Verified UI dashboard availability via headless browser tests.

---

## [0.9.1] - 2026-02-05

### 🛡️ Major Stability & Self-Healing Update
This release addresses critical production issues regarding order duplication and bot "amnesia" (state desync), introducing the first fundamental self-healing capabilities to the core engine.

### ✨ New Features

#### Phase 11: Fundamental Self-Healing (Smart Adoption)
- **DeepReconciler Upgrade** (`engine/reconciler.py`)
  - Added `_reconcile_positions()`: Automatically detects orphan positions on the exchange (where Bot state is IDLE but Position exists).
  - New **Smart Adoption** logic: Validates ownership of orphan positions via local `bot_orders` history before re-adoption.
  - Automatically restores DB state (Step, Investment, Entry) upon detection, allowing the bot to resume managing the trade without manual intervention.
  
#### Deterministic Order IDs V2 (`engine/bot_executor.py`)
- **Robust ID Generation**: Replaced random/time-based ID components with strictly deterministic indices.
- **Forced Integer Casting**: Hardened `_gen_id_v2` to prevent hexadecimal string pollution in Client Order IDs, ensuring reliable exchange de-duplication.
- **System-Wide Alignment**: Applied V2 IDs across Entry, TP, and Grid order types.

### 🧹 Maintenance & Sanitation
- **Database Purge**: Removed ~89,000 redundant `DEBUG_LOG` entries from `trade_history` to restore UI performance and history clarity.
- **Legacy Order Migration**: Automated logic to identify and cancel legacy non-deterministic orders, allowing the bots to replace them with conformant V2 orders.
- **Diagnostics Cleanup**: Purged over 8 development/diagnostic scripts from the root directory to sanitize the production environment.

### 🐛 Bug Fixes
- **The "Amnesia" Bug**: Resolved issue where cancelling malformed orders caused bots to permanently lose track of active positions.
- **Order Spam/Time Bug**: Fixed generation of new IDs every second which caused the exchange to accept multiple duplicate orders.
- **Grid ID Corruption**: Fixed a bug where `grid_step` was being incorrectly cast, leading to malformed IDs that UI filters would ignore.

---


## [0.9.0] - 2026-02-04

### 🎉 Major Release: Advanced Analytics & Risk Management

This release represents a significant evolution of the platform with comprehensive analytics, advanced risk management, and enhanced strategy capabilities.

### ✨ New Features

#### Phase 10.1: Strategy Enhancements
- **Multi-Timeframe Trend Analysis** (`engine/strategies/martingale_strategy.py`)
  - `check_mtf_trend()`: Confirm trend across 4h, 1d, and 4h timeframes
  - Prevents counter-trend entries for improved win rate
  
- **Volatility-Based Position Sizing** (`engine/strategies/martingale_strategy.py`)
  - `calculate_volatility_sizing()`: Automatically adjusts lot size based on ATR
  - Reduces position size in high volatility environments (risk management)
  
- **Correlation Filtering** (`engine/indicators.py`, `engine/strategies/martingale_strategy.py`)
  - New `correlation()` function for pair correlation analysis
  - `correlation_check()`: Avoid trading correlated pairs simultaneously
  - Reduces portfolio concentration risk

#### Phase 10.2: Risk Management
- **Daily Loss Limits** (`engine/bot_management.py`, `engine/bot_executor.py`)
  - `check_daily_loss()`: Monitors cumulative daily losses
  - Automatically pauses trading when threshold exceeded
  - Configurable per-bot via UI
  
- **Drawdown Protection** (`engine/bot_management.py`)
  - `check_drawdown_reduction()`: Monitors unrealized P/L
  - Triggers partial position close when drawdown exceeds configured percentage
  - Helps lock in profits and limit losses
  
- **Portfolio Risk Visualization** (`ui/views/monitor.py`)
  - Interactive heatmap showing risk distribution across active positions
  - Color-coded by martingale step (risk level)
  - Size represents capital invested

#### Phase 10.3: Analytics Dashboard
- **New Analytics Page** (`ui/views/analytics.py`)
  - Comprehensive performance metrics: Win Rate, Profit Factor, Expectancy
  - Visual equity curve showing account growth over time
  - Profit/Loss distribution histogram
  - Per-bot performance breakdown
  
- **Trade History Export** (`engine/metrics.py`)
  - `export_trade_history()`: Export complete trade journal to CSV
  - Includes all trade details: entry/exit prices, P/L, timestamps
  - Enables external analysis in Excel/Python
  
- **Enhanced Metrics** (`engine/metrics.py`)
  - Prometheus metrics server for monitoring
  - Real-time bot health tracking
  - P/L aggregation and reporting

### 🐛 Bug Fixes
- **IndentationError in Bot Manager** (`ui/views/bot_manager.py:889`)
  - Fixed inconsistent indentation in Risk Management section
  - Standardized to 4-space indentation throughout
  
- **TypeError in Analytics** (`ui/views/analytics.py:55`)
  - Fixed mixed data types in PnL column (float/string)
  - Added explicit type conversion with error handling
  
- **Streamlit Deprecation Warnings** (All UI files)
  - Updated 16 instances of `use_container_width=True` to `width='stretch'`
  - Files: `monitor.py` (9), `analytics.py` (4), `bot_creator.py` (2), `bot_manager.py` (1)
  - Eliminates deprecation warnings in Streamlit 1.x

### 🎨 UI/UX Improvements
- **Professional Light Theme** (`ui/app.py`)
  - Clean, GitHub-inspired color scheme
  - Improved readability and contrast
  - Consistent styling across all pages
  
- **4-Page Navigation**
  - 📊 Live Monitor: Real-time bot status and positions
  - 🏗️ Bot Creator: Strategy configuration wizard
  - 🛠️ Bot Manager: Edit existing bots
  - 📈 Analytics: Performance dashboard (NEW)
  
- **Enhanced Bot Manager** (`ui/views/bot_manager.py`)
  - Removed debug logging for cleaner production output
  - Improved form layout and organization
  - Better error handling and user feedback

### 🔧 Technical Improvements
- **Code Quality**
  - All Python files pass syntax validation
  - Removed debug print statements
  - Improved error handling and logging
  
- **Dependencies** (`requirements.txt`)
  - Added `requests` for HTTP verification
  - Updated to include all required packages
  - Documented optional dependencies
  
- **Testing** (`tests/verify_ui.py`)
  - New HTTP-based UI verification script
  - Automated error detection in running app
  - Alternative to browser-based testing

### 📝 Documentation
- **README.md**: Complete rewrite for v0.9.0
  - Updated feature list with Phase 10 additions
  - Comprehensive project structure documentation
  - Improved getting started guide
  - Security best practices
  
- **CHANGELOG.md**: This file
  - Detailed v0.9.0 release notes
  - Migration guide from v0.4.1
  
- **Walkthrough.md**: Phase 10 implementation summary
  - Step-by-step feature implementation
  - Verification results
  - Known issues and resolutions

### 🔄 Migration from v0.4.1

#### Database Changes
No schema changes required. Existing database is fully compatible.

#### Configuration Changes
New optional parameters in bot configuration:
```python
# Risk Management (optional, defaults to 0/disabled)
MaxDrawdownPct: float  # Trigger partial close at X% unrealized loss
DailyLossLimit: float  # Pause trading after X% daily loss

# Strategy Enhancements (optional, defaults to False)
UseMTFTrend: bool      # Enable multi-timeframe trend filter
UseVolSizing: bool     # Enable volatility-based position sizing
UseCorrelation: bool   # Enable correlation filtering
```

#### UI Changes
- New "Analytics" tab in sidebar navigation
- Enhanced Bot Manager with additional risk parameters
- Portfolio heatmap in Live Monitor

### ⚠️ Known Issues
1. **Browser Verification Tool**: Environment issue prevents automated browser testing
   - Workaround: Use `tests/verify_ui.py` for HTTP-based verification
   - Manual testing recommended before deployment

2. **WebSocket Frontend**: Real-time updates not yet implemented in UI
   - Backend WebSocket server functional (port 8765)
   - Frontend listener pending (Phase 9.1)

### 📊 Statistics
- **Files Modified**: 12
- **Lines Added**: ~1,500
- **Lines Removed**: ~50
- **New Features**: 10
- **Bug Fixes**: 3
- **Documentation Updates**: 3

### 🙏 Acknowledgments
This release represents the completion of Phase 10 (Advanced Features) and sets the foundation for future enhancements including backtesting integration and multi-exchange support.

---

## [0.4.1] - 2026-01-19

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

**Solution**: Added early exchange position fetching to create unified view

### 🔄 Multi-Bot Order ID Tracking
**Files**: 
- `engine/database.py` - New order tracking functions
- `engine/runner.py` - Save order IDs on placement
- `ui/views/monitor.py` - Show per-bot order breakdown

**Benefits:**
- Each bot manages its own orders
- Cancel/Modify only affects that bot's orders
- Clear visibility: "Bot A has this order, Bot B has that order"
- Manual orders marked separately

### 📊 Streamlit API Fixes
**File**: `ui/views/monitor.py`

Fixed deprecated `st.column_global_config` → `st.column_config`

### 🧪 Playwright Tests
**File**: `tests/test_pl_sync.py` (NEW)

Comprehensive test suite covering positions sync, orders, ATR values, and default settings.

---

## Quick Fix for P/L Sync Issue

If your bots show "In Trade" but exchange shows no positions:

```bash
# Option 1: Restart runner (triggers sync on startup)
python -m engine.runner

# Option 2: Run cleanup script
python cleanup_ghost_trades.py
```

---

## Testing Checklist

- [x] ATR shows different values for 4h, 1d, 3d, 5d
- [x] Running bots show P/L correctly
- [x] Open Positions (Exchange) matches bot state
- [x] Sync status indicator shows SYNCED
- [x] Default settings: 20x leverage, 1.8 martingale, 1.5% TP, 1.1 ATR grid
- [x] Analytics page loads without errors
- [x] Trade history export works
- [x] Risk management features functional
- [x] No deprecation warnings in logs
