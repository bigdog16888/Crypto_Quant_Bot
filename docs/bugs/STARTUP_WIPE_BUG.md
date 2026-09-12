# Startup Wipe Bug — Hedge Children Incorrectly Reset on Engine Restart

**Status**: ✅ **FIXED** (v4.1.6, Option A) | **Severity**: High | **Discovered**: 2026-09-12 | **Fixed**: 2026-09-12 | **Affects**: All hedge children on engine restart

---

## What Triggers It

Every time the engine restarts, `startup_sync()` runs and calls `seal_all_active_bots()` (ledger.py:1132), which iterates **all active bots** — including hedge children — and calls `seal_trade_state(bot_id)` on each.

`seal_trade_state()` → `_seal_trade_state_internal()` → `_reset_bot_after_tp_internal()` unconditionally calls `safe_mark_reset_cleared(..., allow_nonzero_wipe=True)` at database.py:1867 because `action_label='TP_HIT'` is in `excluded_carry_labels`.

This **bypasses the wipe guard** and marks all `filled` orders for that bot as `reset_cleared` — even for hedge children that:
- Have valid entry fills
- Have never had a TP event in the current cycle
- Have no parent TP sync yet

---

## Why It's Structural (Fires on Every Restart)

| Property | Evidence |
|----------|----------|
| **Not a one-time event** | Runs in `startup_sync()` which executes on **every engine start** |
| **Not pair-specific** | Affects **any hedge child** that has an entry fill but no TP event in its current cycle |
| **Self-reinforcing** | The wipe marks the order `reset_cleared` → `recompute_invested_from_orders` excludes it → `trades.open_qty` stays 0 → next restart sees same state → wipes again |
| **Independent of market state** | Triggered purely by engine startup sequence, not by market conditions or fills |

**Confirmed cases**: SOLUSDC bot 100315 (2026-09-12), SUIUSDC bot 10018 cycle sweep (systemic), potentially any hedge child on any restart.

---

## Root Cause Location

```
startup_sync()                          [runner/startup.py:306]
  └─ seal_all_active_bots()             [ledger.py:1132]
       └─ seal_trade_state(bot_id)      [ledger.py:691]
            └─ _seal_trade_state_internal()
                 └─ _reset_bot_after_tp_internal()  [database.py:1734]
                      └─ safe_mark_reset_cleared(    [wipe_proof.py:56]
                           ...,
                           allow_nonzero_wipe=True    <-- BUG: unconditional for TP_HIT
                       )
```

**Key line**: `database.py:1623-1643` — `allow_nonzero_wipe=(action_label in excluded_carry_labels)` evaluates `True` for `TP_HIT` even when called from `seal_all_active_bots()` on a bot that never had a TP.

---

## Fix Applied (Option A — Minimal Guard)

**File**: `engine/database.py:1623-1643`

```python
# 🚀 STARTUP-WIPE GUARD (v4.1.6): Only allow nonzero wipe if bot
# actually had a TP fill in the current cycle. Prevents startup_sync()
# from wiping hedge children / bots without real TP events.
has_real_tp = cursor.execute("""
    SELECT 1 FROM bot_orders 
    WHERE bot_id = ? AND order_type = 'tp' AND status = 'filled' 
    AND cycle_id = ? AND filled_amount > 0
    LIMIT 1
""", (bot_id, new_cycle)).fetchone()
allow_wipe = (action_label in excluded_carry_labels) and has_real_tp

safe_mark_reset_cleared(
    cursor=cursor,
    bot_id=bot_id,
    symbol=pair,
    action_label=action_label,
    fetch_exchange_position_fn=_fetch_pos_wrapper,
    excluded_carry_labels=excluded_carry_labels,
    now_ts=now_ts,
    allow_nonzero_wipe=allow_wipe
)
```

**Effect**: 
- Bots WITH real TP fills in current cycle → wipe ALLOWED (legitimate TP reset)
- Bots WITHOUT real TP fills in current cycle → wipe BLOCKED (hedge children, idle bots)

---

## Verification

**Test**: `tests/test_startup_wipe_guard.py` — **4/4 tests PASSED**

| Test Case | Result |
|-----------|--------|
| Bot 100315 (hedge child, no TP in cycle 15) | ✅ PROTECTED |
| Bot 10018 (parent, TP in cycle 25 but `reset_cleared`) | ✅ PROTECTED |
| Bot with filled TP in current cycle | ✅ ALLOWED |
| Real startup_simulation | ✅ PROTECTED |

---

## Related Items

- **SUI Cycle Sweep Bug** — `docs/bugs/SUI_CYCLE_SWEEP_BUG.md` (systemic cycle-sweep over-aggression)
- **Daily Shutdown Self-Healing** — `docs/DAILY_SHUTDOWN_SELF_HEALING.md`

---

## Historical Record

**Before Fix** (2026-09-12):
- SOLUSDC bot 100315: entry fill wiped to `reset_cleared` on every restart, parity gap −0.6 SHORT
- SUIUSDC bot 10018: 93 fills swept to `reset_cleared` across 25 cycles, parity gap +111.5 LONG

**After Fix** (2026-09-12):
- SOLUSDC: **0.0 gap** (hedge child protected)
- SUIUSDC: cycle sweep logic fixed separately (see `SUI_CYCLE_SWEEP_BUG.md`)