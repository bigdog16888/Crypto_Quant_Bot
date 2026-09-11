# Hedge-Live-Guard Hardening Design Doc

**Date:** 2026-08-28  
**Incident:** LINK cascade 13:48–14:13 UTC — hedge-live-guard rewrote `trades.open_qty` 12× based on single noisy `fetch_positions()` reads after DNS failure at startup. Two phantom rows marked `filled` with `filled_at=0` (no real exchange fill).

---

## Root Cause

### What Happened
1. Engine restart at 12:06 UTC — **DNS resolution failed** for `demo-fapi.binance.com` (all LINK API calls errored)
2. At 12:11/12:13, GTR reported LINK as orphan `-31.03` (exchange position confirmed)
3. At **13:48**, first hedge-live-guard run — `fetch_positions()` returned **LONG 143.23** (not SHORT -31.03)
4. Hedge-live-guard **trusted single read**, rewrote child `open_qty` to 143.23, inserted `LIVE_GUARD` phantom marker
5. Subsequent runs (every ~30s): `fetch_positions()` returned **cycling values** 109.30 → 68.12 → 103.16 → 174.26 LONG
6. Each read → DB rewrite + new phantom row
7. At 14:13:08–09, two phantom rows marked **`status=filled`** with `filled_at=0` — **no real fill event**

### Why It Was Harmful
- Testnet `fetch_positions()` returns ephemeral/phantom quantities (known testnet behavior)
- Single-read trust + no rate-of-change bound = DB corruption cascade
- Phantom rows marked `filled` without real fills = ledger integrity violation
- Engine was in degraded connectivity state (DNS failure at startup) — should have been in "safe mode"

---

## Current Code Location

`engine/bot_executor.py` — `HEDGE-LIVE-GUARD-INV30` logic (approx lines 2880–2930)

Key flow:
```python
# Current (vulnerable) logic:
live_hedge_qty = fetch_positions_for_child_side()  # SINGLE READ
if live_hedge_qty > child_step_qty + tolerance:
    update_trades_open_qty(child_id, live_hedge_qty)
    insert_live_guard_marker_row()
```

---

## Proposed Fix Options

### Option A: Multi-Read Corroboration (Recommended)
Require **N consistent reads** within a time window before acting.

| Parameter | Proposed Value | Rationale |
|-----------|----------------|-----------|
| `HEDGE_LIVE_GUARD_MIN_READS` | 3 | 3 consistent reads = high confidence |
| `HEDGE_LIVE_GUARD_READ_WINDOW_SEC` | 10 | 3 reads in 10s = ~3-4s apart |
| `HEDGE_LIVE_GUARD_QTY_TOLERANCE` | 0.01 (1%) | Allow minor jitter between reads |
| `HEDGE_LIVE_GUARD_STARTUP_COOLDOWN_SEC` | 300 (5 min) | After any startup connectivity failure, don't act for 5 min |

**Logic:**
```python
# Pseudocode
if startup_connectivity_failure_recent(cooldown_sec=300):
    log("HEDGE-LIVE-GUARD: Startup cooldown active, skipping")
    return

readings = []
for _ in range(MIN_READS):
    qty = fetch_positions_for_child_side()
    readings.append(qty)
    sleep(READ_WINDOW_SEC / MIN_READS)

# Check consistency
if max(readings) - min(readings) > QTY_TOLERANCE * median(readings):
    log("HEDGE-LIVE-GUARD: Readings inconsistent, skipping")
    return

live_hedge_qty = median(readings)
# ... proceed with existing logic
```

**Pros:** Simple, deterministic, catches testnet noise
**Cons:** Adds latency (10s) to hedge-live-guard path

---

### Option B: Rate-of-Change Bound
Track history of `fetch_positions()` values; reject changes exceeding physical limits.

| Parameter | Proposed Value | Rationale |
|-----------|----------------|-----------|
| `HEDGE_LIVE_GUARD_MAX_QTY_CHANGE_PER_MIN` | 50% | Max realistic position change per minute |
| `HEDGE_LIVE_GUARD_HISTORY_LEN` | 10 | Keep last 10 readings |
| `HEDGE_LIVE_GUARD_STARTUP_COOLDOWN_SEC` | 300 | Same as Option A |

**Logic:**
```python
# Pseudocode
current_qty = fetch_positions_for_child_side()
history = get_recent_hedge_qty_history(child_id, limit=10)

if history:
    last_qty = history[-1]
    max_change = last_qty * MAX_QTY_CHANGE_PER_MIN / 60 * elapsed_sec
    if abs(current_qty - last_qty) > max_change:
        log(f"HEDGE-LIVE-GUARD: Rate of change exceeded ({current_qty} vs {last_qty}), skipping")
        return

record_hedge_qty_history(child_id, current_qty)

if startup_cooldown_active():
    return

# ... proceed with existing logic
```

**Pros:** No added latency, adapts to real position changes
**Cons:** More complex, needs persistent history storage

---

### Option C: Hybrid (Recommended for Production)
Combine both: **multi-read corroboration** for immediate action, **rate-of-change** for ongoing monitoring, **startup cooldown** always.

| Parameter | Value |
|-----------|-------|
| `HEDGE_LIVE_GUARD_MIN_READS` | 3 |
| `HEDGE_LIVE_GUARD_READ_WINDOW_SEC` | 10 |
| `HEDGE_LIVE_GUARD_QTY_TOLERANCE` | 0.01 |
| `HEDGE_LIVE_GUARD_MAX_QTY_CHANGE_PER_MIN` | 50% |
| `HEDGE_LIVE_GUARD_STARTUP_COOLDOWN_SEC` | 300 |

---

## Test Plan (Using LINK Cascade as Fixture)

### Test Fixture: `tests/fixtures/hedge_live_guard_link_cascade.json`
```json
{
  "scenario": "LINK cascade 2026-08-28",
  "startup_connectivity_failure": true,
  "fetch_positions_sequence": [
    {"ts": 1787896082, "qty": 143.23, "expected_action": "skip (cooldown)"},
    {"ts": 1787896312, "qty": 143.23, "expected_action": "skip (cooldown)"},
    {"ts": 1787896815, "qty": 109.30, "expected_action": "skip (cooldown)"},
    {"ts": 1787896917, "qty": 68.12, "expected_action": "skip (inconsistent)"},
    {"ts": 1787896917, "qty": 103.16, "expected_action": "skip (inconsistent)"},
    {"ts": 1787896917, "qty": 174.26, "expected_action": "skip (inconsistent)"},
    {"ts": 1787897348, "qty": 68.12, "expected_action": "skip (inconsistent)"},
    {"ts": 1787897379, "qty": 103.16, "expected_action": "skip (inconsistent)"},
    {"ts": 1787897379, "qty": 174.26, "expected_action": "skip (inconsistent)"}
  ],
  "expected_db_writes": 0,
  "expected_phantom_rows_inserted": 0
}
```

### Test Cases

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `test_startup_cooldown_blocks` | DNS failure at startup → hedge-live-guard runs within 5 min | No DB writes, no phantom rows |
| `test_inconsistent_reads_block` | 3 reads: 100, 200, 50 (same cycle) | No DB write, logs inconsistency |
| `test_consistent_reads_allow` | 3 reads: 100.0, 100.5, 99.8 (within 1%) | DB write allowed |
| `test_rate_of_change_blocks` | History: 100 → next read 200 (100% jump in 30s) | Blocked, logged |
| `test_real_change_allowed` | History: 100 → 105 → 110 (gradual, within 50%/min) | Allowed |
| `test_filled_integrity` | Any path marking `status=filled` | Must have real exchange fill ID + `filled_at > 0` |

### Test Implementation
```python
# tests/test_hedge_live_guard_hardening.py
def test_link_cascade_fixture():
    """Replay actual LINK cascade — verify zero DB corruption."""
    with patch_fetch_positions(link_cascade_sequence):
        engine = start_engine(excluded_bots=[10020, 100320])
        run_cycles(10)
        
        # Verify
        assert count_live_guard_rows(100320) == 0
        assert trades_open_qty(100320) == 0
        assert no_rows_marked_filled_without_real_fill()

def test_filled_requires_real_fill():
    """No row can reach status=filled without exchange fill evidence."""
    with patch("engine.exchange_interface.ExchangeInterface.fetch_my_trades", return_value=[]):
        hedge_live_guard_run()
        # All LIVE_GUARD rows must be reset_cleared or open, never filled
```

---

## Acceptance Criteria

1. **LINK cascade fixture** → 0 DB writes, 0 phantom rows, 0 `filled` without real fill
2. **Real hedge scenario** (gradual position change) → allows legitimate corrections
3. **Startup DNS failure** → 5-min cooldown honored
4. **All existing tests pass** (no regression on legitimate hedge-live-guard cases)

---

## Implementation Notes

- Add config keys to `config/settings.py`
- History storage: in-memory deque (per child bot) + persisted to `bot_state` table on shutdown
- Cooldown: track `startup_connectivity_failure_ts` in `bot_state` or global config
- `filled_at` enforcement: add CHECK constraint or application-level guard in `save_bot_order`

---

## Decision Required

Select option (A, B, or C) before implementation. Option C (hybrid) recommended.