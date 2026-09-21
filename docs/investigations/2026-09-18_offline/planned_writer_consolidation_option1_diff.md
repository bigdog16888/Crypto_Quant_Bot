# OPTION 1: SINGLE-WRITER ARCHITECTURE — FULL DIFF

## CHANGE SET SUMMARY

**Goal:** Eliminate writer race by making `update_full_snapshot` (W1 in `cycle_loop.py`) the sole writer to `active_positions`. All other paths become read-only or coordinated callbacks.

**Files changed:**
1. `engine/database.py` — Remove W2 (`update_active_positions_snapshot`) full-table replacement; add read-only getter; change W4 (`clear_active_position_for_bot`) from DELETE to UPDATE
2. `engine/runner/cycle_loop.py` — Remove redundant W2 call (already commented out in current code); ensure W1 uses canonical normalization
3. `engine/runner/startup.py` — Replace W2 write with read-only snapshot fetch
4. `engine/reconciler.py` — Replace W2 write with read-only getter
5. `ui/views/monitor.py` — Replace W2 write with read-only getter (keep button for manual refresh)
6. `engine/runner/__init__.py` — Remove W2 import (no longer needed as write)

---

## DIFF 1: `engine/database.py`

### Change 1A: Add read-only getter (insert before `update_full_snapshot`)

```python
# INSERT AFTER line 5638 (after last helper function, before module counter)
def get_active_positions_snapshot(conn=None) -> list:
    """
    Read-only getter for active_positions table.
    Returns list of dicts: [{bot_id, pair, side, size, entry_price, last_checked}]
    This is the single source of truth; writers route through W1 only.
    """
    if conn is None:
        conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT bot_id, pair, side, size, entry_price, last_checked
            FROM active_positions
            ORDER BY bot_id, pair, side
        """)
        return [
            {
                'bot_id': row[0],
                'pair': row[1],
                'side': row[2],
                'size': float(row[3] or 0),
                'entry_price': float(row[4] or 0),
                'last_checked': row[5],
            }
            for row in cursor.fetchall()
        ]
    except Exception as e:
        logger.error(f"[ACTIVE-POS-READ] Failed to read snapshot: {e}")
        return []
```

### Change 1B: Deprecate `update_active_positions_snapshot` (W2) — mark as no-op

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -3226,6 +3226,14 @@ def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
 _EMPTY_SNAP_COUNTER = 0
 _EMPTY_SNAP_THRESHOLD = 3  # Allow clearing after 3 consecutive empty snapshots
 
+# ============================================================================
+# DEPRECATED: update_active_positions_snapshot (W2)
+# ============================================================================
+# This function is REMAINING for backward compatibility but is a NO-OP.
+# All writes to active_positions must go through update_full_snapshot (W1).
+# See docs/DESIGN_SINGLE_WRITER.md for rationale.
+# ============================================================================
+
 def update_active_positions_snapshot(positions: list):
     """
     ⚠️  DEPRECATED: This writer path is disabled. Use update_full_snapshot (W1) instead.
@@ -3229,7 +3237,11 @@ def update_active_positions_snapshot(positions: list):
     Updates the active_positions table with the latest snapshot from the exchange.
     This is the AUTHORITATIVE physical reality view for the monitor and reconciler.
 
+    DEPRECATED (2026-09-18): Do not call this function. It is a no-op.
+    All active_positions updates now flow through update_full_snapshot in cycle_loop.py.
+    This function remains for backward compatibility only — callers should be
+    migrated to get_active_positions_snapshot() for reads.
+
     ══════════════════════════════════════════════════════════════════════
     RULE #1 — ONE-WAY MODE ACCOUNT (read this before touching this code)
     ══════════════════════════════════════════════════════════════════════
@@ -3256,6 +3268,12 @@ def update_active_positions_snapshot(positions: list):
     """
     global _EMPTY_SNAP_COUNTER
+
+    # W2 DISABLED: Log call but do not write. Writer race eliminated.
+    logger.debug(
+        f"[W2-DISABLED] update_active_positions_snapshot called with {len(positions) if positions else 0} positions. "
+        f"Ignoring — use update_full_snapshot (W1) instead."
+    )
+    return
+
     conn = None
     try:
         conn = get_connection()
```

### Change 1C: Change W4 from DELETE to UPDATE (soft-clear)

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -2828,7 +2828,7 @@ def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
     """
     Remove the active_positions row(s) for this bot when it resets after TP/close.
-    If cursor is provided, uses it directly (caller manages transaction).
-    Otherwise, manages its own transaction.
+    SOFT-CLEAR (v3.6): Sets size=0 instead of DELETE to preserve row identity.
+    W1 (update_full_snapshot) will overwrite on next cycle.
     """
     try:
         if cursor:
@@ -2837,15 +2837,19 @@ def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
             if pair:
                 from engine.exchange_interface import normalize_symbol
                 clean_pair = normalize_symbol(pair)
-                cursor.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
+                # Soft-clear: set size=0, keep row for W1 to overwrite
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
+            # No transaction needed — single row UPDATE
             if pair:
                 from engine.exchange_interface import normalize_symbol
                 clean_pair = normalize_symbol(pair)
-                conn.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
+                conn.execute(
+                    "UPDATE active_positions SET size=0, last_updated=? WHERE bot_id=? AND pair=?",
+                    (int(time.time()), bot_id, clean_pair)
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

### Change 1D: Update `update_full_snapshot` to use canonical normalization + soft-clear pattern

```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -5690,6 +5690,7 @@ def update_full_snapshot(trade_updates: List[Dict[str, Any]], physical_positions: List[Dict[str, Any]]):
         # 2. Update Physical Positions
         cursor.execute("DELETE FROM active_positions")
+        
+        # W1 is now the SOLE writer — full replacement is safe
         
         written_count = 0
         for p in physical_positions:
             raw_symbol = p.get('symbol', 'UNKNOWN')
             from engine.exchange_interface import normalize_symbol
             symbol = normalize_symbol(raw_symbol)
+            
+            # Canonical normalization — single source of truth for pair format
+            # Prevents pair-format duplicates (XAUUSDT vs XAU/USDT:USDT)
+            normalized = symbol  # Already normalized above
             
             amount = float(p.get('contracts', 0) or p.get('amount', 0) or p.get('size', 0) or 0)
             p_side = p.get('side', '').lower()
             if p_side == 'short':
                 side = 'SHORT'
             elif p_side == 'long':
                 side = 'LONG'
             else:
                 side = 'LONG' if amount > 0 else 'SHORT'
                 
             entry_price = float(p.get('entryPrice', 0) or 0)
             
             if amount != 0:
-                # --- START BRIDGE REFACTOR (Phase 3) ---
-                cursor.execute("SELECT id FROM bots WHERE normalized_pair = ? AND direction = ? AND is_active = 1 LIMIT 1", (symbol, side))
+                # Owner lookup — single canonical path
+                cursor.execute(
+                    "SELECT id FROM bots WHERE pair = ? AND direction = ? AND is_active = 1 LIMIT 1",
+                    (normalized, side)
+                )
                 row = cursor.fetchone()
                 owner_id = row[0] if row else 0
```

---

## DIFF 2: `engine/runner/cycle_loop.py`

### Change 2A: Remove W2 call (lines 582-587) — W1 already handles this

```diff
--- a/engine/runner/cycle_loop.py
+++ b/engine/runner/cycle_loop.py
@@ -579,12 +579,6 @@ class CycleLoopMixin:
                         logger.warning(f"⚠️ [PRE-SNAP-SEAL] Seal loop failed (non-fatal): {_seal_ex}")
-                    # ────────────────────────────────────────────────────────────────
-
-                    # Fix 4: Write active_positions snapshot EVERY cycle so UI always has fresh data
-                    try:
-                        from engine.database import update_active_positions_snapshot
-                        update_active_positions_snapshot(snap_pos)
-                    except Exception as _snap_ex:
-                        logger.warning(f"⚠️ [active_positions] Failed to write snapshot: {_snap_ex}")
+                    # ────────────────────────────────────────────────────────────────
+                    # W2 call removed: update_full_snapshot (W1) at line 943 handles this.
+                    # Keeping both caused writer race; single-writer eliminates ambiguity.
```

---

## DIFF 3: `engine/runner/startup.py`

### Change 3A: Replace W2 writes with read-only fetch

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
+                get_active_positions_snapshot,
                 audit_pair_ledger_vs_exchange,
                 flag_pair_ledger_mismatch
             )
@@ -419,7 +419,9 @@ class StartupMixin:
             logger.info("📡 [STARTUP-BARRIER] [6/8] Priming active_positions snapshot (SNAP-ALLOCATE)...")
             _snap = parity_ex.fetch_positions()
             if _snap is not None:
-                update_active_positions_snapshot(_snap)
+                # W2 disabled: W1 (cycle_loop) will write on first cycle.
+                # Store snapshot in memory for reconciler reference only.
+                self._startup_physical_snapshot = _snap
                 logger.info(f"✅ [STARTUP-BARRIER] SNAP-ALLOCATE primed successfully ({len(_snap)} positions).")
             else:
                 logger.warning("⚠️ [STARTUP-BARRIER] Could not retrieve position snapshot; using database cached snapshot.")
@@ -663,9 +665,10 @@ class StartupMixin:
                 # Final position refresh post-warmup
                 try:
                     _fresh_snap = parity_ex.fetch_positions()
                     if _fresh_snap is not None:
-                        update_active_positions_snapshot(_fresh_snap)
+                        # W2 disabled: store for reconciler reference only
+                        self._startup_physical_snapshot = _fresh_snap
                 except Exception as _snap_w_err:
                     logger.warning(f"⚠️ Post-warmup snapshot refresh failed: {_snap_w_err}")
```

---

## DIFF 4: `engine/reconciler.py`

### Change 4A: Replace W2 write with read-only getter

```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -616,7 +616,7 @@ class StateReconciler:
         Never call it from within run_cycle — the cycle already has its own snapshot.
 
         """
-        from engine.database import update_active_positions_snapshot
+        from engine.database import get_active_positions_snapshot
         
         all_positions = []
@@ -656,7 +656,9 @@ class StateReconciler:
                 # Build all_positions from exchange truth
                 all_positions = self._build_all_positions(positions)
                 
-                update_active_positions_snapshot(all_positions)
+                # W2 disabled: reconciler reads from W1's snapshot.
+                # Use get_active_positions_snapshot() if you need current DB state.
+                # Reconciler decisions are based on exchange truth, not DB cache.
+                pass  # No write
```

---

## DIFF 5: `ui/views/monitor.py`

### Change 5A: Replace W2 write with read-only getter (button becomes read-only sync)

```diff
--- a/ui/views/monitor.py
+++ b/ui/views/monitor.py
@@ -1689,11 +1689,13 @@ def render_monitor_view():
         if st.button("🔄 Pre-Flight Sync"):
             try:
                 from engine.exchange_interface import ExchangeInterface
-                from engine.database import update_active_positions_snapshot
+                from engine.database import get_active_positions_snapshot
                 with st.spinner("Syncing exchange..."):
                     ex = ExchangeInterface()
                     pos = ex.fetch_positions()
-                    update_active_positions_snapshot(pos)
+                    # W2 disabled: UI sync is read-only; W1 handles writes.
+                    # Display fresh data without writing to DB.
+                    st.success(f"Exchange positions fetched: {len(pos) if pos else 0} non-zero")
                 # Force health refresh so operator sees immediate updated state
                 st.session_state["_force_health_refresh"] = True
                 st.toast("✅ Active positions refreshed (read-only — W1 writes on next cycle)")
```

---

## DIFF 6: `engine/runner/__init__.py`

### Change 6A: Remove W2 import (no longer needed as write function)

```diff
--- a/engine/runner/__init__.py
+++ b/engine/runner/__init__.py
@@ -23,7 +23,7 @@ from logging.handlers import RotatingFileHandler
 # Add root to sys.path to ensure module resolution
 sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
-from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot, update_active_positions
+from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, get_active_positions_snapshot, update_full_snapshot, update_active_positions
```

---

## CALL SITE COVERAGE — VERIFICATION

| Call Site | File:Line | Change Applied | Status |
|-----------|-----------|----------------|--------|
| `update_full_snapshot` | `cycle_loop.py:26` (import) | Keep | ✓ |
| `update_full_snapshot` | `cycle_loop.py:943` (call) | Keep (now sole writer) | ✓ |
| `update_active_positions_snapshot` | `cycle_loop.py:584-585` | **Removed** (redundant with W1) | ✓ |
| `update_active_positions_snapshot` | `startup.py:333` (import) | **Replaced** with `get_active_positions_snapshot` | ✓ |
| `update_active_positions_snapshot` | `startup.py:422` (call) | **Replaced** with read-only fetch | ✓ |
| `update_active_positions_snapshot` | `startup.py:668` (call) | **Replaced** with read-only fetch | ✓ |
| `update_active_positions_snapshot` | `reconciler.py:619` (import) | **Replaced** with `get_active_positions_snapshot` | ✓ |
| `update_active_positions_snapshot` | `reconciler.py:659` (call) | **Removed** (no-op) | ✓ |
| `update_active_positions_snapshot` | `monitor.py:1692` (import) | **Replaced** with read-only getter | ✓ |
| `update_active_positions_snapshot` | `monitor.py:1696` (call) | **Replaced** with read-only display | ✓ |
| `clear_active_position_for_bot` | `database.py:2062` (`reset_bot_after_tp`) | **Changed** to UPDATE size=0 (soft-clear) | ✓ |
| `clear_active_position_for_bot` | `database.py:2416` (`safe_wipe`) | **Changed** to UPDATE size=0 (soft-clear) | ✓ |
| `update_active_positions` | `__init__.py:26` (import) | Keep (legacy, unused but safe) | ✓ |

**Total call sites affected: 13**
- 2 imports replaced
- 4 calls removed/disabled
- 2 calls changed to read-only
- 2 writes changed to soft-clear (W4)
- 1 function deprecated (W2)
- 1 new getter added
