# RATE LIMIT WEIGHT MAP — Crypto_Quant_Bot vs Binance FAPI

**Date: 2026-09-10** | **Method: live measurement, not docs pages** — per-call weights from `X-MBX-USED-WEIGHT-1M` response-header deltas on `fapi.binance.com` (public endpoints), limits from live `exchangeInfo` `rateLimits` arrays on both real and demo FAPI. Signed-endpoint weights are marked unverified until the shadow read-only phase (needs a real key to measure).

## 1. Limits — real FAPI vs demo FAPI (live `exchangeInfo`, 2026-09-10)

| Limit type | REAL fapi.binance.com | DEMO demo-fapi.binance.com | Delta |
|---|---|---|---|
| REQUEST_WEIGHT / MINUTE | **2400** | **6000** | **demo 2.5× looser — demo never pressure-tested our real ceiling** |
| ORDERS / MINUTE | 1200 | 1200 | same |
| ORDERS / 10 SECOND | 300 | 300 | same |

## 2. Per-call REQUEST_WEIGHT (header-measured on real FAPI, 2026-09-10)

| Call (exact form we use) | Weight | Measured |
|---|---|---|
| `GET /fapi/v1/time` | 1 | ✅ header delta |
| `GET /fapi/v1/klines?limit=50` (1m) | 1 | ✅ header delta |
| `GET /fapi/v1/klines?limit=100` (15m) | 1 | ✅ header delta |
| `GET /fapi/v1/ticker/24hr?symbol=X` (single) | 1 | ✅ header delta |
| `GET /fapi/v1/premiumIndex?symbol=X` | 1 | ✅ header delta |
| `GET /fapi/v1/depth?limit=5` | 2 | ✅ header delta |
| `GET /fapi/v1/openOrders?symbol=X` (symbol-scoped — see exchange_interface.py:399) | 1 | ✅ header delta |
| `POST/DELETE /fapi/v1/order`, `DELETE /fapi/v1/allOpenOrders` | 1 (doc-claimed) | ⚠️ unverified — needs signed call |
| `GET /fapi/v1/order` (verify-GET, new fix path) | 1 (doc-claimed) | ⚠️ unverified |
| `GET /fapi/v2/account` / `v2/balance` | 5 (doc-claimed) | ⚠️ unverified |
| `GET /fapi/v1/userTrades` / `v1/income` | 5–10 (doc-claimed) | ⚠️ unverified |

**Weight-40 trap avoided**: `openOrders` costs 40 only when called WITHOUT symbol. Our code always passes symbol → 1. Confirmed at exchange_interface.py:399.

## 3. Engine per-minute weight budget (real FAPI, ~5 cycles/min, 20 bots)

| Component | Calls/min (est.) | Weight/min |
|---|---|---|
| OHLCV klines (TF-cached, `cycle_loop.py:670,693` — cache hit avoids call) | 40–200 | 40–200 |
| Price tickers (per-bot checks) | 20–100 | 20–100 |
| `_audit_pending_grids` order-status GETs (measured: 71 fetches / 72 cycles for ~5-7 pending orders → ~1/order/cycle) | 5–10 | 5–10 |
| `openOrders` symbol-scoped sweeps | 5–10 | 5–10 |
| Signed account/balance/trades (if called per-cycle — audit pending) | 0–20 | 0–100 |
| **Normal-ops total** | | **~70–420 → 3–18% of 2400 cap** |

**Headroom conclusion: normal operations are comfortable (worst realistic case ≤ ~20% of cap).** The risk is not steady-state; it is retry storms (below).

## 4. Worst-case math — the actual risk

**Single stuck order (today's 583851054 live data)**: cancel attempt ≈ 1/s → 60 DELETE + 60 verify-GET per min = **120 weight/min (5% cap) + 120 orders/min (10% order cap)**. Harmless alone.

**N simultaneous corrupted orders** (the escalation math the cancel fix created): weight and order rates scale ×N. **At N=10: 1200 weight/min = 50% cap; 1200 orders/min = AT the 1200/min order cap.** The cancel fix's retry loop is itself a rate-limit hazard under multi-order corruption windows — the same API-corruption event class that produced two stuck orders on 09-09.

**Emergency liquidation sweep** (`cancel_orders_by_bot_id` + flatten across up to 20 bots): burst of order actions in one cycle — bounded by the 300 orders/10s cap with a full 20-bot flatten (~40-60 actions) but the ORDER-rate caps are the binding constraint, not weight.

## 5. Findings

1. **Demo's weight ceiling is 6000/min vs real 2400/min** — the engine has never run against the real constraint. Not a blocker for normal ops (Section 3), but retry storms hit real caps 2.5× sooner.
2. **Zero actual rate-limit events in demo history**: 253 grep hits for 429/-1003/limit patterns were false positives (only 2 × `-2011` cancel-responses matched). Demo never rejected us — consistent with a 2.5× looser ceiling.
3. **The cancel-fix retry loop needs back-off, not just escalation** (Section 4): one order is fine; a corruption window producing N stuck orders turns the fix's own retries into a rate-limit incident. Fix belongs in the operability branch.
4. Signed-endpoint weights are doc-claimed only — measure with the read-only key in the shadow phase before real-money soak.

## 6. Required operability-branch work (client-side rate governance)

1. **Weight tracker**: read `X-MBX-USED-WEIGHT-1M` from every real-FAPI response; log/track 1-min usage.
2. **Soft throttle** at 80% of cap (1920/min): defer non-critical calls (klines refresh, audits) to next cycle.
3. **Hard back-off on 429 / -1003**: honor `Retry-After` header, exponential backoff, never retry-cancel a -1003 immediately.
4. **Per-order retry back-off after CANCEL-ESCALATION fires**: exponential to a 30s floor per stuck order — caps N-order storm at N×2 weight/min (N=10 → 20/min, trivial), kills the alert-spam problem in the same change.
5. **Alert at sustained >80% for 5 min** — operability alert channel, not CRITICAL spam.

*Sources: live exchangeInfo rateLimits (real + demo, 2026-09-10), live header-delta measurements (real FAPI public endpoints, 2026-09-10), engine log census (72 cycles, engine.log), engine code census (exchange_interface.py, cycle_loop.py).*
