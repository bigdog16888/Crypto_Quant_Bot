# Item 5 — audit_bot_wipes Wrong Signature: Root-Cause Analysis + Proposed Fix

## Current State (Verified)
- File: `engine/reconciler_wipe_audit.py` lines 82-87
- Call site: `engine/reconciler.py:3851`
- **Signature mismatch confirmed**

## Root Cause Trace

### Function Signatures

**Public API (line 82-87):**
```python
def audit_bot_wipes(bot_id: int, symbol: str, exchange_gap: float) -> WipeAuditResult:
    from engine.write_queue import WriteQueue
    return WriteQueue().put_and_wait(_audit_bot_wipes, bot_id, symbol, exchange_gap)
```

**Internal function (line 62-79):**
```python
def _audit_bot_wipes(cursor, bot_id: int, symbol: str, exchange_gap: float) -> WipeAuditResult:
    suspect_data = _check_recompute_for_suspects(cursor, bot_id, symbol)
    # ...
```

### Call Site (reconciler.py:3851)
```python
with get_connection() as conn:
    cur = conn.cursor()
    for bot_obj in bots:
        audit = audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)
```

**Arguments passed:** `(cursor, bot_id, symbol, exchange_gap)` — **4 arguments**
**Function expects:** `(bot_id, symbol, exchange_gap)` — **3 arguments**

### What Happens at Runtime
Python passes `cur` as `bot_id` (first positional), `bot_obj.bot_id` as `symbol` (second), `pair_normalized` as `exchange_gap` (third), and `global_diff` is **extra** (TypeError: takes 3 positional arguments but 4 were given).

**But wait** — the code uses `WriteQueue().put_and_wait()` which wraps the call. Let me check if the error is caught.

Looking at line 3857-3859:
```python
except Exception as e:
    logger.error(f"Error during wipe audit: {e}")
```

The TypeError would be caught and logged as "Error during wipe audit: ...", but the audit would silently fail. The wipe audit trap would never run.

### Why This Wasn't Caught
1. The call is inside a try/except that swallows all exceptions
2. The audit is advisory (logging only) — not blocking
3. No test directly calls this path with real data

## Proposed Fix

### Option 1: Fix Call Site to Match Public API (Recommended)
The public API is designed to run on WriteQueue (thread-safe). The call site already has a connection and cursor — it should use the internal function directly.

```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -3841,11 +3841,11 @@ async def reconcile_all(
                 try:
 
                     from .reconciler_wipe_audit import audit_bot_wipes
 
                     with get_connection() as conn:
 
                         cur = conn.cursor()
 
                         for bot_obj in bots:
 
-                            audit = audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)
+                            from .reconciler_wipe_audit import _audit_bot_wipes
+                            audit = _audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)
 
                             if audit.probable_cause_match:
```

### Option 2: Fix Public API to Accept Cursor (Not Recommended)
This would break the WriteQueue pattern and require all callers to manage connections.

## Evidence of Impact
- The wipe audit trap (V3.3.1 feature) has **never executed successfully**
- Any reconciliation mismatch that could be explained by unproved wipes goes undetected
- This is a silent failure — no crash, no alert, just missing diagnostic capability

## Test Plan
1. Add unit test calling `_audit_bot_wipes` directly with known suspect rows
2. Add integration test triggering reconcile_all with a bot that has legacy wipes
3. Verify `probable_cause_match` logic works (gap matches suspect qty within 0.1%)
4. Full regression suite pass

## Proposed Diff

```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -3838,12 +3838,12 @@ async def reconcile_all(
 
                 # 🚀 V3.3.1: WIPE AUDIT TRAP
 
                 try:
 
-                    from .reconciler_wipe_audit import audit_bot_wipes
+                    from .reconciler_wipe_audit import _audit_bot_wipes
 
                     with get_connection() as conn:
 
                         cur = conn.cursor()
 
                         for bot_obj in bots:
 
-                            audit = audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)
+                            audit = _audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)
 
                             if audit.probable_cause_match:
 
                                 logger.error(f"💎 [WIPE-MATCH] Identified suspect unproved wipes on Bot {bot_obj.bot_id} totaling {audit.total_suspect_qty:.4f}. This matches the system mismatch of {global_diff:.4f}!")
 
                 except Exception as e:
 
                     logger.error(f"Error during wipe audit: {e}")
```

## Priority
**MEDIUM** — Silent diagnostic failure, not a money-path bug. Fix in next batch after P1 items.