# Item 2 — Stale-Cycle_ID Dedup Wedge: Root-Cause Analysis + Proposed Fix

## Current State (Verified)
- Bot 10008 (SOL): `trades.cycle_id=39`, `open_qty=0`, `active_positions=0.23 LONG`
- Bot 10018 (SUI): `trades.cycle_id=31`, `open_qty=0`, `active_positions=58.6 LONG`
- Both `is_active=0`, `status=STOPPED`
- Cycle_id_filter_fix applied (commit 78f5600) — fixed `wipe_wall_ts` filter on current cycle

## Root Cause Trace

### The Dedup Wedge Mechanism
The "stale-cycle_id dedup wedge" refers to a scenario where:
1. A bot has filled orders in cycle N
2. Cycle advances to N+1 (Mechanism B)
3. The filled orders in cycle N are NOT closed (no TP fills)
4. `recompute_invested_from_orders(bot_id, cycle_id=N+1)` is called
5. Auto-detected `cycle_floor` finds cycle N (or earlier) as first unbalanced
6. FIFO matching from cycle_floor to N+1 consumes cycle N entries against historical exits
7. Result: `open_qty=0` for cycle N+1, but physical position remains open
8. The stale `cycle_id` in `trades` (now N+1) "wedges" the dedup logic — the function thinks it's computing for a clean cycle but pulls in historical baggage

### Detailed Flow (database.py:4473-4680)
```python
# Line 4492: target_cycle = trades.cycle_id (e.g., 39 for SOL, 31 for SUI)
# Line 4514-4533: cycle_floor auto-detection
#   - Scans cycles < target_cycle for unbalanced entry/exit
#   - For SOL: finds cycle 16 (first cycle with unclosed position)
#   - For SUI: finds cycle 25 (first cycle with massive grid entries)
# Line 4549: entries WHERE cycle_id >= cycle_floor AND cycle_id <= target_cycle
# Line 4571: exits WHERE cycle_id >= cycle_floor AND cycle_id <= target_cycle
# FIFO matching: all entries 16-39 matched against all exits 16-39
# Cycle 19-20 entries (0.23 SOL) consumed by cycle 16-18 exits
# Result: net 0 for target_cycle=39
```

### Why cycle_id_filter_fix (78f5600) Doesn't Fully Close This
The fix addressed `wipe_wall_ts` filter on **current cycle** (`cycle_id=None`):
```python
# Before: effective_wall_ts = wipe_wall_ts for all cycles
# After: effective_wall_ts = 0 for cycle_id=None (current), wipe_wall_ts for historical
```
This fixes `recompute_invested_from_orders(bot_id, cycle_id=None)` — the live cycle call.

**But the dedup wedge occurs when:**
- `trades.cycle_id` has already advanced to N+1
- The call is `recompute_invested_from_orders(bot_id, cycle_id=N+1)` (explicit historical cycle)
- `cycle_floor` auto-detection still pulls in cycles < N+1
- The function is SUPPOSED to compute position for cycle N+1 only, but includes historical cycles

### The Core Design Flaw
`recompute_invested_from_orders` has dual purpose:
1. **Live cycle** (`cycle_id=None`): Compute current position from all relevant history
2. **Historical cycle** (explicit `cycle_id`): Compute position AS OF that cycle

The current code conflates these. For historical cycle, it should ONLY consider fills in that cycle (or at most that cycle + CARRY from previous), not all cycles from auto-detected floor.

## Proposed Fix

### Option A: Explicit Cycle Isolation (Recommended)
When explicit `cycle_id` is passed, compute ONLY that cycle's net (entries - exits in that cycle), ignoring historical cycles entirely. CARRY logic only applies to live cycle.

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -4490,6 +4490,15 @@ def recompute_invested_from_orders(
         target_cycle = row_trade[0]  # trades.cycle_id
 
+    # If explicit cycle_id was passed, compute ONLY that cycle (historical isolation)
+    # cycle_floor auto-detection and historical FIFO are for live cycle only
+    if cycle_id is not None:
+        target_cycle = cycle_id
+        cycle_floor = cycle_id  # Only this cycle
+        effective_wall_ts = 0   # No timestamp filter for historical
+    else:
+        # Live cycle: existing logic with cycle_floor auto-detection
+        pass
```

### Option B: Cycle Floor Respects Cycle Boundaries
Modify auto-detection to not cross cycle boundaries where the cycle was "properly closed" (had matching entry/exit volume).

```diff
# In cycle_floor auto-detection (lines 4514-4533):
# Add check: if a cycle has net_qty ≈ 0 (within tolerance), it's "closed" — stop scanning further back
```

## Evidence That Fix Is Needed
Even after 78f5600, bots 10008/10018 still have:
- `trades.open_qty=0` but `active_positions>0`
- The orphan positions are in cycles 19-20 (SOL) and cycle 31 (SUI) — cycles that were NOT the current cycle when cycle advanced
- Next engine start will call `recompute_invested_from_orders` with explicit cycle_id from trades — will still return 0

## Test Plan
1. Add test: `recompute_invested_from_orders(bot_id=10008, cycle_id=39)` returns 0.23 (not 0)
2. Add test: `recompute_invested_from_orders(bot_id=10018, cycle_id=31)` returns 58.6 (not 0)
3. Verify live cycle (`cycle_id=None`) still works correctly
4. Full regression suite pass

## Proposed Diff (Option A — Minimal, Targeted)

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -4485,7 +4485,16 @@ def recompute_invested_from_orders(
     cursor = conn.cursor()
 
     # Get trade state for this bot
     row_trade = cursor.execute(
         "SELECT cycle_id, wipe_wall_ts, open_qty, total_invested FROM trades WHERE bot_id = ?",
         (bot_id,)
     ).fetchone()
 
-    target_cycle = row_trade[0] if row_trade else 0
+    # Determine target cycle and floor
+    if cycle_id is not None:
+        # Explicit historical cycle: isolate to that cycle only
+        target_cycle = cycle_id
+        cycle_floor = cycle_id
+        effective_wall_ts = 0
+    else:
+        # Live cycle: use trades.cycle_id with auto-detected floor
+        target_cycle = row_trade[0] if row_trade else 0
+        # ... existing cycle_floor auto-detection logic follows ...
```

## Priority
**HIGH** — This is the root cause of the SOL/SUI orphans. Fix before any adoption/flatten decision for 10008/10018.