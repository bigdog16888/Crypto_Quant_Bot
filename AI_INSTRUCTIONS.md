# AI Handover Instructions: Crypto Quant Bot

## 🛠️ Architecture & Conventions

### 1. UI Keys & State Isolation (CRITICAL)
- **Constraint**: Streamlit tabs maintain all widgets in the global DOM. Duplicate labels/keys cause crashes.
- **Convention**: 
    - Use `create_*` prefix for all keys in `ui/views/bot_creator.py`.
    - Use `edit_*_{bot_id}` as a prefix for all keys in `ui/views/bot_manager.py`'s `render_edit_form`.
    - Always pass a unique `key` to common widgets like `cci_tf`, `rsi_level`, etc.

### 2. The 11-Trigger Confluence System
- **Location**: `engine/strategies/mql4_strategy.py` -> `check_signals`.
- **Logic**:
    - **Triggers 1-4**: Indicators (CCI, Boll, Stoch, RSI).
    - **Triggers 5-8**: **Indicator-Aware Patterns**. Mode 1=Up, 2=Down. Can watch `Price`, `RSI`, or `CCI` over X candles.
    - **Trigger 9**: Price Threshold. Hard filter for specific price levels.
    - **Trigger 10**: Volatility Relative Percentile. Uses historical lookback to determine if market is Quiet or Extreme.
    - **Trigger 11**: ATR Expansion. Distance from candle open as % of ATR range.
- **Strict Confluence**: A signal is only returned if `triggers_active > 0` and **ALL** enabled triggers are concurrently `True`.

### 3. Risk Projections & Math transparency
- **Logic**: `MQL4Strategy.calculate_projections(base_price, current_atr)`
- **Projections**: Detailed absolute prices for grid entries and TP targets.
- **Costs**: Includes 0.1% fee + 0.05% slippage simulation.
- **TP Price**: Calculated as `Breakeven + (Target Profit USD / Total Position Qty)`.

### 4. Automated Hedging & Runner State
- **Runner**: `engine/runner.py` branches between:
    - Signal Hunting (`check_signals`).
    - Active Management (`manage_trade`).
- **Manager**: `engine/manager.py` handles TP, Martingale Grids, and the **Automated Hedge Executor**.
- **Re-entry**: Supports post-exit cooldown (time) and distance-based re-entry.

### 5. Database Schema
- `bots`: Static configuration.
- `trades`: Active position tracking.
- `last_exit_price` & `last_exit_time`: Tracked in `trades` for re-entry logic.

## 📌 Development Roadmap
- [x] Phase 10: 8-Trigger Entry System
- [x] Phase 11: Real-World Risk (Fees, Hedging)
- [x] Phase 12: Advanced Entry (9-11 Triggers, Re-entry logic)
- [x] Phase 13: UI Transparency (Absolute Price Projections, Indicator-Aware Patterns)
- [ ] Phase 14: Live Exchange Integration (CCXT Live Orders)
- [ ] Phase 15: Market Maker Logic Refinement

---
*End of Protocol.*
