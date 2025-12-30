# AI Handover Instructions: Crypto Quant Bot

## 🛠️ Architecture & Conventions

### 1. UI Keys & State Isolation (CRITICAL)
- **Constraint**: Streamlit tabs maintain all widgets in the global DOM. Duplicate labels/keys cause crashes.
- **Convention**: 
    - Use `create_*` prefix for all keys in `ui/views/bot_creator.py`.
    - Use `edit_*_{bot_id}` as a prefix for all keys in `ui/views/bot_manager.py`'s `render_edit_form`.
    - Always pass a unique `key` to common widgets like `cci_tf`, `rsi_level`, etc.

### 2. The 8-Trigger Confluence System
- **Location**: `engine/strategies/mql4_strategy.py` -> `check_signals`.
- **Logic**:
    - **Triggers 1-4**: Indicators (CCI, Boll, Stoch, RSI). Support `0=Off`, `1=Above/DN`, `2=Below/UP`.
    - **Triggers 5-8**: Pattern Slots (`pat_1` to `pat_4`). Independent Count and Timeframe.
    - **Strict Confluence**: A signal is only returned if `triggers_active > 0` and **ALL** enabled triggers are concurrently `True`.

### 3. Risk Projections & Fees
- **Logic**: `MQL4Strategy.calculate_projections`
- **Simulation**: Uses a `cost_factor` (1.0 + 0.001 fee + 0.0005 slippage = 1.0015).
- **Output**: Returns `total_invested_usdc` which includes cumulative trading costs.

### 4. Automated Hedging & Runner State
- **Runner**: `engine/runner.py` branches between:
    - `is_in_trade == False`: Hunting for signals using `check_signals`.
    - `is_in_trade == True`: Managing active trade using `manage_trade`.
- **Manager**: `engine/manager.py` -> `manage_trade` handles:
    - Real-time TP monitoring.
    - Automated Grid Step execution.
    - **Automated Hedge Executor**: Triggers based on `check_hedge_entry` (Step or DD% threshold).

### 5. Database Schema
- `bots`: stores static config.
- `trades`: stores live position data (`current_step`, `total_invested`, `avg_entry_price`, `target_tp_price`).
- `bot_id` in `trades` is a primary key linking to `bots.id`.

## 📌 Pending Tasks
- [ ] Connect `exchange.create_order` (currently DRY_RUN logs only).
- [ ] Implement actual position fetching from exchange in `emergency_close_all`.
- [ ] Add more granular "Market Maker" logic variants.

---
*End of Protocol.*
