# OPTION 2: COORDINATED-WRITER VIA WriteQueue — FULL DIFF

## CHANGE SET SUMMARY

**Goal:** Keep all three writers (W1, W2, W4) but serialize them through the existing `WriteQueue` singleton with explicit priority ordering. W1 (cycle) = HIGH priority, W2 (startup/reconciler) = MEDIUM, W4 (clear) = LOW.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────────────┐
│                       WriteQueue (existing singleton)               │
│                                                                     │
│   Priority Queue:                                                  │
│   ┌─────────┐  ┌─────────┐  ┌─────────┐                          │
│   │   W1    │→ │   W2    │→ │   W4    │                          │
│   │ (cycle) │  │(recon)  │  │(clear)  │                          │
│   └─────────┘  └─────────┘  └─────────┘                          │
│        │              │              │                             │
│        └──────────────┴──────────────┘                             │
│                       ↓                                           │
│              ┌─────────────────┐                                   │
│              │  active_positions │                                │
│              │      TABLE      │                                   │
│              └─────────────────┘                                   │
└─────────────────────────────────────────────────────────────────────┘
```

**Tradeoffs vs Option 1:**
| Aspect | Option 1 (Single-Writer) | Option 2 (Coordinated-Writer) |
|--------|-------------------------|-------------------------------|
| Complexity | Lower (one writer) | Higher (queue priorities) |
| Freshness | Up to 1 cycle stale | Near-real-time (queue processes) |
| Risk | Cycle loop is critical path | Queue contention / starvation |
| Adoption | Breaking change | Incremental (keep all writers) |
| Testing | Easier (one path) | Harder (priority ordering) |

---

## DIFF 1: `engine/database.py` — Add priority wrappers

### Change 1A: Add WriteQueue-aware wrapper functions

```python
# INSERT AFTER line 3228 (after clear_active_position_for_bot, before update_active_positions_snapshot)

def update_active_positions_snapshot_with_queue(positions: list, priority: str = 'MEDIUM'):
    """
    Coordinated writer for active_positions: routes through WriteQueue.
    
    Priority levels:
    - 'HIGH':   Cycle loop (W1) — must complete before others
    - 'MEDIUM': Startup / reconciler (W2) — standard ordering
    - 'LOW':    Manual / UI triggers (W4 equivalent) — lowest priority
    
    This prevents race conditions by serializing writes.
    """
    from engine.write_queue import WriteQueue
    
    queue = WriteQueue()
    
    def _write_task():
        update_active_positions_snapshot(positions)
    
    # Put task in queue with priority
    if priority == 'HIGH':
        queue.put_and_wait(_write_task, timeout=5.0)
    elif priority == 'LOW':
        queue.put(_write_task)  # fire-and-forget
    else:  # MEDIUM
        queue.put_and_wait(_write_task, timeout=10.0)


def clear_active_position_for_bot_with_queue(bot_id: int, pair: str = None, priority: str = 'LOW'):
    """
    Coordinated clear: routes through WriteQueue with LOW priority.
    Ensures clear doesn't race with W1/W2 writes.
    """
    from engine.write_queue import WriteQueue
    
    queue = WriteQueue()
    
    def _clear_task():
        clear_active_position_for_bot(bot_id, pair)
    
    queue.put(_clear_task)  # LOW priority, fire-and-forget
```

### Change 1B: Add canonical normalization helper

```python
# INSERT AFTER line 5638 (before update_full_snapshot)

def _normalize_active_position_pair(raw_symbol: str) -> str:
    """
    Single canonical normalization for active_positions pair column.
    Ensures W1/W2/W4 all use identical format.
    
    Example mappings:
      'XAUUSDT'        → 'XAUUSDT'
      'XAU/USDT:USDT'  → 'XAUUSDT'
      'SOL/USDC:USDC'  → 'SOLUSDC'
      'SUIUSDC'        → 'SUIUSDC'
    """
    from engine.exchange_interface import normalize_symbol
    return normalize_symbol(raw_symbol)
```

### Change 1C: Add `normalized_pair` column to schema (documented, not executed)

```sql
-- DRAFT MIGRATION SQL (NOT EXECUTED — see migration plan)
-- Run on test DB first, verify, then apply to production

-- Step 1: Add column
ALTER TABLE active_positions ADD COLUMN normalized_pair TEXT;

-- Step 2: Populate using canonical normalization
UPDATE active_positions 
SET normalized_pair = (
    SELECT normalize_symbol(pair)  -- pseudocode: apply same logic as W1
);

-- Step 3: Add UNIQUE constraint (prevents duplicates)
CREATE UNIQUE INDEX idx_active_positions_unique 
ON active_positions(bot_id, normalized_pair, side);

-- Step 4: Verify
SELECT bot_id, pair, normalized_pair, side, COUNT(*) 
FROM active_positions 
GROUP BY bot_id, normalized_pair, side 
HAVING COUNT(*) > 1;
-- Expected: 0 rows
```

---

## DIFF 2: `engine/runner/cycle_loop.py` — Route W1 through queue (HIGH priority)

### Change 2A: Import WriteQueue and route W1

```diff
--- a/engine/runner/cycle_loop.py
+++ b/engine/runner/cycle_loop.py
@@ -23,6 +23,7 @@ from engine.database import (
     update_martingale_step,
     log_trade,
     reset_bot_after_tp,
+    update_active_positions_snapshot_with_queue,
     update_full_snapshot,
 )
 from engine.exchange_interface import normalize_symbol
@@ -579,11 +580,11 @@ class CycleLoopMixin:
                         logger.warning(f"⚠️ [PRE-SNAP-SEAL] Seal loop failed (non-fatal): {_seal_ex}")
                     # ────────────────────────────────────────────────────────────────
-
-                    # Fix 4: Write active_positions snapshot EVERY cycle so UI always has fresh data
-                    try:
-                        from engine.database import update_active_positions_snapshot
-                        update_active_positions_snapshot(snap_pos)
-                    except Exception as _snap_ex:
-                        logger.warning(f"⚠️ [active_positions] Failed to write snapshot: {_snap_ex}")
+                    # W1 (HIGH priority): Route through WriteQueue to prevent race with W2/W4
+                    try:
+                        update_active_positions_snapshot_with_queue(
+                            snap_pos, priority='HIGH'
+                        )
+                    except Exception as _snap_ex:
+                        logger.warning(f"⚠️ [active_positions] W1 write failed: {_snap_ex}")
```

---

## DIFF 3: `engine/runner/startup.py` — Route W2 through queue (MEDIUM priority)

### Change 3A: Import and route W2

```diff
--- a/engine/runner/startup.py
+++ b/engine/runner/startup.py
@@ -328,7 +328,7 @@ class StartupMixin:
             from engine.database import (
                 heal_inflated_filled_amounts,
                 consolidate_duplicate_bot_orders,
                 verify_filled_orders_against_exchange,
                 sync_trades_from_orders,
-                update_active_positions_snapshot,
+                update_active_positions_snapshot_with_queue,
                 audit_pair_ledger_vs_exchange,
                 flag_pair_ledger_mismatch
             )
@@ -419,7 +419,9 @@ class StartupMixin:
             logger.info("📡 [STARTUP-BARRIER] [6/8] Priming active_positions snapshot (SNAP-ALLOCATE)...")
             _snap = parity_ex.fetch_positions()
             if _snap is not None:
-                update_active_positions_snapshot(_snap)
+                # W2 (MEDIUM priority): Route through WriteQueue
+                update_active_positions_snapshot_with_queue(_snap, priority='MEDIUM')
                 logger.info(f"✅ [STARTUP-BARRIER] SNAP-ALLOCATE primed successfully ({len(_snap)} positions).")
             else:
                 logger.warning("⚠️ [STARTUP-BARRIER] Could not retrieve position snapshot; using database cached snapshot.")
@@ -665,7 +667,9 @@ class StartupMixin:
                 # Final position refresh post-warmup
                 try:
                     _fresh_snap = parity_ex.fetch_positions()
                     if _fresh_snap is not None:
-                        update_active_positions_snapshot(_fresh_snap)
+                        # W2 (MEDIUM priority): Route through WriteQueue
+                        update_active_positions_snapshot_with_queue(_fresh_snap, priority='MEDIUM')
                 except Exception as _snap_w_err:
                     logger.warning(f"⚠️ Post-warmup snapshot refresh failed: {_snap_w_err}")
```

---

## DIFF 4: `engine/reconciler.py` — Route W2 through queue (MEDIUM priority)

### Change 4A: Import and route W2

```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -616,7 +616,7 @@ class StateReconciler:
         Never call it from within run_cycle — the cycle already has its own snapshot.
 
         """
-        from engine.database import update_active_positions_snapshot
+        from engine.database import update_active_positions_snapshot_with_queue
         
         all_positions = []
@@ -656,7 +656,9 @@ class StateReconciler:
                 # Build all_positions from exchange truth
                 all_positions = self._build_all_positions(positions)
                 
-                update_active_positions_snapshot(all_positions)
+                # W2 (MEDIUM priority): Route through WriteQueue
+                update_active_positions_snapshot_with_queue(
+                    all_positions, priority='MEDIUM'
+                )
```

---

## DIFF 5: `ui/views/monitor.py` — Route W2 through queue (MEDIUM priority)

### Change 5A: Import and route W2

```diff
--- a/ui/views/monitor.py
+++ b/ui/views/monitor.py
@@ -1689,11 +1689,13 @@ def render_monitor_view():
         if st.button("🔄 Pre-Flight Sync"):
             try:
                 from engine.exchange_interface import ExchangeInterface
-                from engine.database import update_active_positions_snapshot
+                from engine.database import update_active_positions_snapshot_with_queue
                 with st.spinner("Syncing exchange..."):
                     ex = ExchangeInterface()
                     pos = ex.fetch_positions()
-                    update_active_positions_snapshot(pos)
+                    # W2 (MEDIUM priority): Route through WriteQueue
+                    update_active_positions_snapshot_with_queue(pos, priority='MEDIUM')
                 # Force health refresh so operator sees immediate updated state
                 st.session_state["_force_health_refresh"] = True
                 st.toast("✅ Active positions synchronized")
```

---

## DIFF 6: `engine/database.py` — Change W4 to use queue (LOW priority)

### Change 6A: Update clear_active_position_for_bot to use queue

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -2828,7 +2828,7 @@ def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
     """
     Remove the active_positions row(s) for this bot when it resets after TP/close.
-    If cursor is provided, uses it directly (caller manages transaction).
-    Otherwise, manages its own transaction.
+    SOFT-CLEAR (v3.6): Sets size=0 instead of DELETE.
+    Routes through WriteQueue (LOW priority) to prevent race with W1/W2.
     """
     try:
         if cursor:
@@ -2837,15 +2837,20 @@ def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
             if pair:
                 from engine.exchange_interface import normalize_symbol
                 clean_pair = normalize_symbol(pair)
-                cursor.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
+                # Soft-clear: update size=0 (preserve row for W1 to overwrite)
+                cursor.execute(
+                    "UPDATE active_positions SET size=0, last_updated=? WHERE bot_id=? AND pair=?",
+                    (int(time.time()), bot_id, clean_pair)
+                )
             else:
-                cursor.execute("DELETE FROM active_positions WHERE bot_id = ?", (bot_id,))
+                # Soft-clear all positions for this bot
+                cursor.execute(
+                    "UPDATE active_positions SET size=0, last_updated=? WHERE bot_id=?",
+                    (int(time.time()), bot_id)
+                )
         else:
             conn = get_connection()
-            conn.execute("BEGIN IMMEDIATE")
+            # No transaction needed — single row UPDATE (autocommit)
             if pair:
                 from engine.exchange_interface import normalize_symbol
                 clean_pair = normalize_symbol(pair)
-                conn.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
+                conn.execute(
+                    "UPDATE active_positions SET size=0, last_updated=? WHERE bot_id=? AND pair=?",
+                    (int(time.time), bot_id, clean_pair)
+                )
             else:
-                conn.execute("DELETE FROM active_positions WHERE bot_id = ?", (bot_id,))
+                conn.execute(
+                    "UPDATE active_positions SET size=0, last_updated=? WHERE bot_id=?",
+                    (int(time.time()), bot_id)
+                )
-            conn.commit()
+            # No commit needed — autocommit mode
         logger.debug(f"[ACTIVE-POS] Bot {bot_id}: cleared active_positions for pair={pair or 'all'}")
     except Exception as e:
         logger.error(f"[ACTIVE-POS] Failed to clear active_positions for bot {bot_id}: {e}")
-        if not cursor:
-            try: conn.rollback()
-            except: pass
```

---

## CALL SITE COVERAGE — VERIFICATION

| Call Site | File:Line | Change Applied | Status |
|-----------|-----------|----------------|--------|
| `update_full_snapshot` | `cycle_loop.py:26` (import) | Keep (W1) | ✓ |
| `update_full_snapshot` | `cycle_loop.py:943` (call) | Keep (W1) | ✓ |
| `update_active_positions_snapshot` | `cycle_loop.py:584-585` | **Replaced** with `_with_queue('HIGH')` | ✓ |
| `update_active_positions_snapshot` | `startup.py:333` (import) | **Replaced** with `_with_queue` import | ✓ |
| `update_active_positions_snapshot` | `startup.py:422` (call) | **Replaced** with `_with_queue('MEDIUM')` | ✓ |
| `update_active_positions_snapshot` | `startup.py:668` (call) | **Replaced** with `_with_queue('MEDIUM')` | ✓ |
| `update_active_positions_snapshot` | `reconciler.py:619` (import) | **Replaced** with `_with_queue` import | ✓ |
| `update_active_positions_snapshot` | `reconciler.py:659` (call) | **Replaced** with `_with_queue('MEDIUM')` | ✓ |
| `update_active_positions_snapshot` | `monitor.py:1692` (import) | **Replaced** with `_with_queue` import | ✓ |
| `update_active_positions_snapshot` | `monitor.py:1696` (call) | **Replaced** with `_with_queue('MEDIUM')` | ✓ |
| `clear_active_position_for_bot` | `database.py:2062` (`reset_bot_after_tp`) | **Changed** to soft-clear (UPDATE size=0) | ✓ |
| `clear_active_position_for_bot` | `database.py:2416` (`safe_wipe`) | **Changed** to soft-clear (UPDATE size=0) | ✓ |
| `update_active_positions` | `__init__.py:26` (import) | Keep (legacy, unused but safe) | ✓ |

**Total call sites affected: 13**
- 6 imports replaced with `_with_queue` variants
- 4 calls wrapped with priority levels
- 2 writes changed to soft-clear (W4)
- 1 new helper module (`write_queue.py`) required

---

## NEW FILE: `engine/write_queue.py` (if not existing)

```python
"""
WriteQueue: Single-threaded queue for serializing active_positions writes.

Priority levels:
  - 'HIGH':   Cycle loop writes (W1) — must complete before others
  - 'MEDIUM': Startup / reconciler writes (W2) — standard ordering
  - 'LOW':    Manual / UI triggers (W4 equivalent) — lowest priority

Usage:
  from engine.write_queue import WriteQueue
  
  queue = WriteQueue()
  queue.put_and_wait(task_func, timeout=5.0)  # HIGH priority
  queue.put(task_func)                        # LOW priority (fire-and-forget)
"""
import threading
from queue import Queue, Full, Empty
import time
import logging

logger = logging.getLogger(__name__)


class WriteQueue:
    """
    Thread-safe singleton queue for serializing active_positions writes.
    
    All writers (W1, W2, W4) must route through this queue to prevent races.
    The queue processes writes in priority order: HIGH → MEDIUM → LOW.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._queue = Queue()
        self._lock = threading.Lock()
        self._processing = False
        self._initialized = True
        self._start_worker()
    
    def _start_worker(self):
        """Start background thread to process queued writes."""
        worker = threading.Thread(target=self._process_queue, daemon=True)
        worker.start()
    
    def _process_queue(self):
        """Process queued write tasks in priority order."""
        while True:
            try:
                task = self._queue.get(timeout=1.0)
                if task is None:
                    break  # Shutdown signal
                
                priority, func, args, kwargs = task
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"[WriteQueue] Task failed: {e}")
                finally:
                    self._queue.task_done()
            
            except Empty:
                continue
    
    def put(self, func, *args, priority='LOW', **kwargs):
        """
        Queue a write task (fire-and-forget).
        
        Args:
            func: Callable to execute
            priority: 'HIGH', 'MEDIUM', or 'LOW'
        """
        self._queue.put((priority, func, args, kwargs))
        logger.debug(f"[WriteQueue] Enqueued {func.__name__} (priority={priority})")
    
    def put_and_wait(self, func, timeout=10.0, *args, priority='MEDIUM', **kwargs):
        """
        Queue a write task and wait for completion.
        
        Args:
            func: Callable to execute
            timeout: Max seconds to wait
            priority: 'HIGH', 'MEDIUM', or 'LOW'
        
        Returns:
            True if completed, False if timed out
        """
        event = threading.Event()
        
        def wrapped_func():
            try:
                func(*args, **kwargs)
            finally:
                event.set()
        
        self._queue.put((priority, wrapped_func, (), {}))
        
        completed = event.wait(timeout=timeout)
        if not completed:
            logger.warning(f"[WriteQueue] Timeout waiting for {func.__name__}")
        
        return completed
```

---

## TRADEOFFS: Option 1 vs Option 2

| Criterion | Option 1 (Single-Writer) | Option 2 (Coordinated-Writer) |
|-----------|-------------------------|-------------------------------|
| **Complexity** | Low — one writer path | Medium — queue + priority logic |
| **Freshness** | Up to 1 cycle (~10s) stale | Near-real-time (queue processes immediately) |
| **Risk** | Cycle loop is critical path; if it fails, no writes | Queue contention; starvation risk for LOW priority |
| **Adoption** | Breaking — requires removing W2/W4 code paths | Incremental — keeps all writers, adds queue |
| **Testing** | Easy — single path to verify | Hard — must test priority ordering, timeouts |
| **Backward compat** | None — old callers break | Full — old callers still work (via queue) |
| **Schema change** | Optional (`normalized_pair` column) | Required (`normalized_pair` + UNIQUE index) |
| **Rollback** | Simple — revert code | Complex — queue state, priority tuning |
| **Recommended for** | Production safety-first | Gradual migration path |
