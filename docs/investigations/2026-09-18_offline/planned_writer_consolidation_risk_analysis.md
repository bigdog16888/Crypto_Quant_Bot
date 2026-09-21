# RISK DOCUMENT: Option 1 (Single-Writer) — Staleness Failure Scenarios

## OVERVIEW

Under Option 1 (single-writer), `active_positions` is written ONLY by `update_full_snapshot` in `cycle_loop.py` (W1). All other paths (startup, reconciler, monitor) become read-only. The table may be **up to 1 cycle stale** (~10 seconds) between W1 writes.

This document analyzes concrete failure scenarios where staleness could cause bad downstream decisions.

---

## SCENARIO 1: Reconciler Reads Stale Snapshot During Active Trade

### Setup
- Time T=0: Bot 10019 enters LONG position on XAUUSDT (exchange fill confirmed)
- Time T=5s: W1 runs `update_full_snapshot` — writes `active_positions` with correct state
- Time T=10s: Bot 10019 enters GRID order (step 8), fill confirmed on exchange
- Time T=12s: **W1 has NOT run yet** (next cycle at T=20s)
- Time T=15s: Reconciler runs `reconcile_all()`

### What Happens
```python
# In reconciler.py:619
from engine.database import get_active_positions_snapshot  # NEW read-only getter
all_positions = get_active_positions_snapshot()

# all_positions now contains:
#   [{bot_id=10019, pair='XAUUSDT', side='SHORT', size=1.014}]  ← STALE!
#   (Missing the GRID fill at step 8 that happened at T=10s)
```

### Downstream Impact
```python
# Reconciler compares:
#   - active_positions (stale): size=1.014
#   - exchange_fills (fresh):  size=1.266 (1.014 + 0.252 GRID)
#   - trades cache (fresh):    size=1.266

# Result: Reconciler sees DRIFT = 1.266 - 1.014 = 0.252
# Decision: Flag as "unclaimed position" → potential false alert
```

### Severity: **MEDIUM**
- **False positive**: Reconciler flags drift that doesn't exist
- **Mitigation**: Reconciler already has `ALLOW_FORENSIC_ADOPT=False` guard — won't auto-adopt
- **User impact**: Operator sees warning notification, must manually verify

---

## SCENARIO 2: Monitor Displays Stale Position After TP Hit

### Setup
- Time T=0: Bot 10008 (SOL) has ACTIVE LONG position, size=0.23
- Time T=5s: TP order fills on exchange (position closed)
- Time T=10s: **W1 has NOT run yet** (next cycle at T=20s)
- Time T=15s: User opens Streamlit UI (monitor.py)

### What Happens
```python
# In monitor.py:1692
from engine.database import get_active_positions_snapshot
positions = get_active_positions_snapshot()

# positions still shows:
#   [{bot_id=10008, pair='SOLUSDC', side='LONG', size=0.23}]  ← STALE!
#   (Position was closed at T=5s, but W1 hasn't cleared it yet)
```

### Downstream Impact
```python
# UI displays:
#   Bot 10008: LONG SOL 0.23 @ 142.50  ← WRONG (position closed)
#   Status: IN TRADE                     ← WRONG (should be IDLE)

# User sees:
#   - Bot appears active when it's actually flat
#   - May attempt manual close on non-existent position
#   - Health check shows "system healthy" (no drift detected)
```

### Severity: **LOW**
- **False positive**: UI shows stale position
- **Mitigation**: User can click "🔄 Refresh Now" (reads from exchange directly, bypasses cache)
- **User impact**: Confusion, but no financial risk (position is actually flat)

---

## SCENARIO 3: Startup Barrier Reads Stale Snapshot Before W1 Has Run

### Setup
- Engine restarts after crash
- Time T=0: Startup begins, calls `startup_repair_mismatched_pairs()`
- Time T=5s: Reconciler runs `get_active_positions_snapshot()`
- **W1 has NEVER run in this session** (cycle loop not started yet)

### What Happens
```python
# In startup.py:419
_snap = parity_ex.fetch_positions()  # Fresh from exchange
# _snap = [{'symbol': 'SOL/USDC:USDC', 'contracts': 0.23, ...}]

# But W1 hasn't written to active_positions yet!
# So get_active_positions_snapshot() returns EMPTY or STALE data
```

### Downstream Impact
```python
# Reconciler compares:
#   - exchange_fills (fresh): size=0.23
#   - active_positions (empty): size=0
#   - trades cache (stale from prior session): size=0.23

# Result: Reconciler sees DRIFT = 0.23 - 0 = 0.23
# Decision: Attempt to "adopt" position into bot 10008
#   - But bot 10008 may already have trades.open_qty=0.23 (from prior session)
#   - Double-counting risk if adoption succeeds
```

### Severity: **HIGH**
- **False negative**: Reconciler may under-report drift (thinks position is unclaimed)
- **Risk**: Double-counting if adoption succeeds when bot already tracks position
- **Mitigation**: Startup barrier has `detect_and_repair_global_wipe` guard — blocks adoption if bot already has `total_invested > 0`
- **User impact**: Startup may fail with "pair mismatch" error; operator must intervene

---

## SCENARIO 4: Clear-Active-Position Race During TP Cycle

### Setup
- Time T=0: Bot 10018 (SUI) has ACTIVE LONG position, size=58.6
- Time T=5s: TP order fills, `reset_bot_after_tp()` calls `clear_active_position_for_bot()`
- **W4 (soft-clear) sets size=0 immediately**
- Time T=6s: W1 runs `update_full_snapshot` — reads exchange positions (now flat)
- Time T=6s: W1 **DELETEs** all rows and **re-inserts** from exchange (size=0 for SUI)

### What Happens
```python
# W4 (soft-clear):
#   UPDATE active_positions SET size=0 WHERE bot_id=10018

# W1 (full replacement):
#   DELETE FROM active_positions  ← Removes W4's soft-clear
#   INSERT INTO active_positions ...  ← Re-inserts from exchange (size=0 for flat position)

# Result: Row is correctly size=0 (or absent if exchange returns flat)
```

### Downstream Impact
```python
# No issue: W1's full replacement overwrites W4's soft-clear
# The race is resolved by W1's atomic DELETE+INSERT
```

### Severity: **NONE**
- **Correct behavior**: W1's full replacement wins
- **Mitigation**: Built-in (W1 is atomic)
- **User impact**: None

---

## MITIGATION STRATEGIES

| Scenario | Mitigation | Effectiveness |
|----------|------------|---------------|
| 1 (Reconciler stale) | Reconciler already has `ALLOW_FORENSIC_ADOPT=False` — won't auto-adopt | **HIGH** — prevents false positive from becoming action |
| 2 (Monitor stale) | "Refresh Now" button reads from exchange directly | **HIGH** — user can override cache |
| 3 (Startup stale) | Startup barrier has `detect_and_repair_global_wipe` guard | **MEDIUM** — prevents double-counting but may block startup |
| 4 (Clear race) | W1 atomic DELETE+INSERT overwrites W4 soft-clear | **NONE** — no issue |

---

## RECOMMENDATION

**Option 1 is safe IF:**
1. Cycle loop runs reliably (every ~10s) — no missed cycles
2. Reconciler has `ALLOW_FORENSIC_ADOPT=False` (already true)
3. Operator monitors for false positives during first 10s after each trade

**Option 1 is risky IF:**
1. Cycle loop has known staleness issues (e.g., WebSocket disconnects)
2. Startup barrier is not robust enough to handle empty `active_positions`
3. Operator expects real-time UI accuracy (may see 10s stale data)

**Alternative: Hybrid Approach**
- Keep W1 as primary writer
- Allow W2 (startup/reconciler) to write ONLY if W1 hasn't run in >30s (staleness guard)
- This provides fallback freshness without full writer race

---

## CONCLUSION

Staleness under Option 1 is **manageable but not negligible**. The highest-risk scenario is **startup** (Scenario 3), where W1 has never run and the reconciler must make decisions on empty/stale data.

**Recommended guard:** Add staleness check in startup barrier:
```python
# In startup.py, before calling reconcile_all():
positions = get_active_positions_snapshot()
if not positions or all(p['size'] == 0 for p in positions):
    logger.warning("[STARTUP] active_positions is empty/stale. Running W2 fallback to prime snapshot...")
    update_active_positions_snapshot(fetch_positions())  # One-time W2 write
```

This hybrid approach preserves Option 1's simplicity while mitigating the startup risk.
