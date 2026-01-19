# AI Handover Instructions: Crypto Quant Bot v0.4 (Robust Live)

## 🛠️ Architecture & Roles

### 1. Agent Roles
- **Lead Architect**: Responsible for `ui/views`, user experience, and config validation (e.g. `bot_creator.py` integrity).
- **Senior Backend Engineer**: Owns `engine/runner.py`, `engine/sync.py`, and `engine/database.py`. Focuses on reliability, state consistency, and crash recovery.
- **Security Specialist**: Owns `engine/exchange_interface.py` and `config/settings.py`. Enforces `validate_order`, circuit breakers, and secret sanitization.
- **Quant Analyst**: Owns `engine/strategies/martingale_strategy.py`, `engine/risk.py`, and `engine/manager.py`. Focuses on math correctness (fees, slippage, indicators).

### 2. Core Systems (v0.4 Status)

#### A. The Safety Layer (Security Specialist)
- **Validation**: `ExchangeInterface.validate_order` PRE-CHECKS MinNotional, MinQty, and Precision before any API call.
- **Circuit Breaker**: `BotRunner.check_circuit_breaker` monitors Global Equity. If drawdown > 50%, it trips `engine.emergency` and locks the system.
- **Retry Logic**: Network errors use exponential backoff. Logic errors (insufficient funds) fail fast.

#### B. The Resilience Layer (Backend Engineer)
- **Startup Sync**: `BotRunner.sync_all_bots` calls `engine/sync.py`.
    - **Orphan Handler**: Cancels orders if DB says "Idle" but Exchange has orders.
    - **Ghost Handler**: Closes trade in DB if DB says "In Trade" but Exchange has no orders (TP hit offline).
- **Emergency System**: `handle_emergency_liquidation` supports cancelling orders AND market-closing positions.

#### C. The Strategy Layer (Quant Analyst)
- **11-Trigger Confluence**:
    - Triggers 1-4: Indicators (CCI, Boll, Stoch, RSI).
    - Triggers 5-8: Pattern Slots (Indicator-Aware).
    - Triggers 9-11: Price Threshold, Volatility Percentile, ATR Expansion.
- **Risk Math**: Includes 0.1% Fee + 0.05% Slippage in projections.
- **Manager**: Handles `manage_trade` loop (TP, Grid, Hedge, Decay).

#### D. The Interface Layer (Architect)
- **Keys**: Strict `create_*` vs `edit_*_{id}` isolation.
- **ATR Foundation**: Pre-calculates market context (Vol Percentile) for user planning.

### 3. Database Schema
- `bots`: Config, Strategy Type, Limits.
- `trades`: Active state (`current_step`, `total_invested`, `avg_entry_price`, `target_tp_price`).
- `trades` (v0.3+): Added `last_exit_price`, `last_exit_time` for cooldown logic.

## 📌 Development Roadmap (v0.4 -> v1.0)

### Phase 10: Production Release (v1.0 Candidate)
- [ ] **Testnet Burn-in**: Run on Binance Testnet for 48h.
- [ ] **Performance Logging**: Export `trades` to CSV for PnL analysis.
- [ ] **Final Deployment**: Validate `.env` secrets and remove `DRY_RUN` flag.

### Future (v1.1+)
- [ ] **Websocket Integration**: Replace polling with `ccxt.pro` for sub-second reaction.
- [ ] **Market Maker Logic**: Refine `market_maker.py` for spread capture.

---
*Follow strict "Read-Before-Write" and "Safe Request" protocols.*
