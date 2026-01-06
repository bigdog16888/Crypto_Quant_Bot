# Roadmap v0.4: "Live Readiness" Hardening

This roadmap outlines the critical steps required to transition the Crypto Quant Bot from a functional prototype (v0.3) to a robust, secure, and reliable trading system (v0.4/v1.0 Candidate) capable of live operation on Binance.

## Current Status (v0.4)
- **Strengths:** Logic for strategy, grid, hedging, and decay is implemented. Database persists state. UI allows management.
- **Completed Hardening:**
    - Order Validation (MinNotional/MinQty/Precision).
    - Network Retry Logic (Exponential Backoff).
    - Circuit Breaker (Global Drawdown Protection).
    - State Synchronization (Startup Sync for Orphaned/Ghost orders).

---

## Phase 10: Production Release (v1.0 Candidate)
*Objective: Final verification and live deployment.*

### 10.1. Dry-Run on Live Data (Binance Testnet)
- [ ] **Config Switch:** Add `USE_TESTNET=True` flag in `.env`.
- [ ] **Testnet URL:** Point `ccxt` to Binance Testnet URLs in `ExchangeInterface`.
- [ ] **Validation:** Run the bot for 24-48 hours on Testnet. Verify order placement, grid logic, and error handling without risking real funds.

### 10.2. Performance Logging
- [ ] **Trade History Export:** Add a script/UI button to export `trades` table to CSV.
- [ ] **PnL Calculation:** Add a `daily_pnl` tracking table/log to visualize performance over time.

### 10.3. Deployment Checklist
- [ ] Validated `.env` (no default secrets).
- [ ] `MAX_ORDER_USD` set to safe limit.
- [ ] `minNotional` checks active.
- [ ] Logging set to `INFO` (not `DEBUG`) to save disk space.

---

## Completed Phases

### Phase 8: Hardening & Safety Nets (Completed)
- [x] **Robust Exchange Validation:** `validate_order` checks MinNotional, MinQty, Precision.
- [x] **Circuit Breakers:** `check_circuit_breaker` monitors account equity vs initial baseline.
- [x] **Security:** Max Order limits and sanitized logging.

### Phase 9: Reliability & Resilience (Completed)
- [x] **Network Reliability:** Retry logic in `_safe_request`.
- [x] **State Recovery:** `sync_bot_state` handles Orphaned Orders and Ghost Trades on startup.
- [x] **Emergency System:** `handle_emergency_liquidation` supports market close logic.

---

## Next Steps
1.  **Testnet Verification:** Configure `.env` for Binance Testnet and run a 24h burn-in test.
2.  **Monitor Logs:** Watch `engine.log` for any unexpected "State Mismatch" warnings.
