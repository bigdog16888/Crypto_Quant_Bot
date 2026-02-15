# BUG #6: EMPTY SNAPSHOT - RESOLUTION REPORT
**Date**: 2026-02-10
**Status**: ✅ FIXED (Fundamental Safety Gate Implemented)

---

## 🛑 The Problem
**"Empty Snapshot" Bug**:
1.  `runner.py` fetches positions at cycle start.
2.  Sometimes (due to race condition, cache timing, or API glitch) it returns `[]` (empty list).
3.  Bots read this empty list.
4.  Bots compare with DB (`in_trade=True`).
5.  Bots conclude: "Position is gone! Must have been liquidated/closed."
6.  Bots trigger **Ghost Reset** -> Reset to IDLE.
7.  Result: **False Ghost Fleet** (Bots idle, but position actually exists on exchange).

## 🛡️ The Fundamental Solution
We implemented a **3-Layer Safety Gate** that changes the trust model.

**Rule**: *If the Database expects positions, we DO NOT accept an empty list from the Exchange without a fight.*

### Layer 1: Force Refresh Capability
- **File**: `engine/exchange_interface.py`
- **Change**: Added `force_refresh=True` parameter to `fetch_positions()`.
- **Effect**: Bypasses the internal cache (TTL 3s) and forces a fresh API call.

### Layer 2: Runner Safety Gate (The Wall)
- **File**: `engine/runner.py`
- **Change**: In `run_cycle()`:
    1. Fetch positions.
    2. If result is `[]`:
        - Query DB: "Do we expect active trades?" (`total_invested > 0`)
        - If YES:
            - Log `⚠️ Potential Ghost Fleet detected.`
            - Call `fetch_positions(force_refresh=True)`.
    3. **CRITICAL FAIL-SAFE**:
        - If result is *STILL* `[]` after force refresh:
        - **ABORT THE CYCLE**. (`return 5.0`)
        - Do **NOT** pass the empty list to the bots.
        - Do **NOT** run `verify_state_sync`.

**Impact**: It is now **IMPOSSIBLE** for a bot to auto-reset due to a transient empty snapshot. The cycle will simply pause/skip until the API returns valid data.

### Layer 3: Cache Integrity
- **File**: `engine/exchange_interface.py`
- **Change**: `_coalesced_request` now supports forced invalidation.

---

## 🧪 Verification Logic

**Scenario A: Real Empty Account**
1. DB says 0 bots in trade.
2. Exchange returns `[]`.
3. Safety Gate checks DB -> 0 expected.
4. Snapshot accepted. Bots run normal logic (IDLE). ✅

**Scenario B: The Bug (False Empty)**
1. DB says 12 bots in trade.
2. Exchange returns `[]` (glitch).
3. Safety Gate checks DB -> 12 expected.
4. **Trigger Force Refresh**.
5. If Refresh returns positions -> Proceed normally. ✅
6. If Refresh returns `[]` -> **ABORT CYCLE**.
   - Logs: `🛑 [SAFETY-STOP] Aborting cycle to prevent False Ghost Reset.`
   - Bots remain `IN TRADE`. No reset. ✅

**Scenario C: Real Liquidation (All positions wipeout)**
1. DB says 12 bots in trade.
2. Exchange returns `[]`.
3. Safety Gate retries -> `[]`.
4. Cycle Aborts.
5. **Human Action Required**: If you *actually* got liquidated to 0, you must manually reset the DB or wait for the system to eventually catch up (e.g., if one position comes back, the gate opens).
   - *Note*: This is a tradeoff. We prioritize **protecting existing trades** over recognizing a total wipeout instantly.

---

## 📂 Files Modified
- `engine/runner.py`
- `engine/exchange_interface.py`

## 🚀 Status
**READY FOR PRODUCTION**. The "Ghost Fleet" vulnerability is fundamentally patched.
