# Item 1 — XAU ORDER-SYNC Loop: Root-Cause Analysis + Proposed Fix

## Current State (Verified)
- Bot 10019 (XAU SHORT parent): `active_positions` has 1.014 SHORT @ 4347.43 (matches trades)
- Bot 10019 duplicate AP row: 0.201 SHORT @ 4353.89 (no trades counterpart)
- Bot 100319 (XAU LONG hedge): `active_positions` 1.681 LONG @ 4352.35 (matches trades)
- Bot 100315 (SOL hedge): `active_positions` 0.23 LONG @ 99.806 (matches trades)
- Pair format mismatch: `XAUUSDT` vs `XAU/USDT:USDT`

## Root Cause Trace

### The Loop Path
```
reconciler.py:reconcile_all()
  → reconstruct_offline_fills() (lines 1095-1300)
    → For each pair where physical > virtual:
        → Scan 48h exchange order history for CQB_ fills
        → Inject as bot_orders (so offline-sync picks them up)
        → credit_fill() for each adopted fill
          → dual-write to exchange_fills
          → seal_trade_state()
  → update_active_positions_snapshot() (W2) / update_full_snapshot() (W1)
    → Upserts active_positions from exchange_fills
      → USES exchange's raw symbol (XAUUSDT) NOT normalized
```

### The Double-Write Trigger
1. **Backfill source** (`source='backfill'`): Reconciler scans 48h history, finds exchange fills for XAUUSDT, credits them with `credit_fill()` → writes to `exchange_fills` with `symbol='XAUUSDT'`
2. **Reconciler-uncredited source** (`source='reconciler-uncredited'`): Same reconciler run, `credit_fill()` internal dual-write writes AGAIN with same `exchange_order_id` but different `fill_ts` (time.time())
3. **W1/W2 upsert**: `update_full_snapshot()` reads `exchange_fills` grouped by `symbol` → gets both `XAUUSDT` and `XAU/USDC:USDT` as separate symbols → creates duplicate `active_positions` rows

### Why It Loops
- The duplicate AP row (0.201 SHORT `XAUUSDT`) creates virtual/physical mismatch
- Next reconciler run sees mismatch → re-scans history → re-credits same fills → more duplicates
- The `fill_claims` guard (INV-20) uses `(bot_id, order_id)` but:
  - Backfill uses `exchange_order_id` as key
  - Reconciler-uncredited uses same `exchange_order_id`
  - BUT: dual-write guard in `credit_fill` (ledger.py:631) suppresses second write ONLY when `delta <= 0 and is_cumulative=True`
  - First call: `delta = filled_qty - 0 = positive` → logs to exchange_fills
  - Second call (same cumulative_qty): `delta = filled_qty - filled_qty = 0` → `delta <= 0 and is_cumulative=True` → `_log_fill = False`
  - **However**: The backfill and reconciler-uncredited are SEPARATE `credit_fill` calls, not the same call. Each has its own `existing_fill` lookup. The second call sees `existing_fill = filled_qty` (from first call's bot_orders update) → delta=0 → dual-write suppressed. **BUT** the first call's `fill_ts` = historical timestamp, second call's `fill_ts` = `time.time()` → if dual-write NOT suppressed, different fill_ts.

Wait — let me trace more carefully. The backfill path at reconciler.py:1254 calls `credit_fill()` with `is_cumulative=True`. The dual-write happens INSIDE that call. Then reconciler.py:1279 DIRECTLY calls `record_exchange_fill()` again. That's the Finding 1 double-write.

For XAU specifically: the backfill injects bot_orders, then credit_fill is called. The dual-write guard SHOULD suppress the internal exchange_fills write on the second credit_fill call (delta=0). But the EXPLICIT `record_exchange_fill` at line 1279 bypasses the guard entirely.

## Evidence
```sql
-- From earlier query: bot 10007 (BNB) shows the double-write pattern
exchange_order_id=350133162: backfill ts=1789084225, reconciler-uncredited ts=1789637285
exchange_order_id=350133201: backfill ts=1789085306, reconciler-uncredited ts=1789637285

-- For XAU, check if similar pattern exists:
```

## Proposed Fix

### Primary Fix: Remove Reconciler Double Dual-Write (Finding 1)
**File:** `engine/reconciler.py:1275-1293`
**Action:** Delete lines 1275-1293 (the explicit `record_exchange_fill` call after `credit_fill`)

```diff
-            # Write to exchange_fills audit log
-            from engine.database import record_exchange_fill
-            _pair_row = _credit_cur.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
-            _symbol = _pair_row[0] if _pair_row else 'UNKNOWN'
-            _fill_ts = int(time.time())
-            record_exchange_fill(
-                conn=_credit_conn,
-                exchange_order_id=str(order_id),
-                client_order_id=client_cid or '',
-                symbol=_symbol,
-                side=fill_side,
-                qty=filled_qty,
-                price=avg_price,
-                fill_ts=_fill_ts,
-                source='reconciler-uncredited',
-                bot_id=bot_id,
-                order_type=order_type,
-                step=step,
-                cycle_id=cycle_id,
-            )
```

**Why this fixes XAU loop:** The duplicate `exchange_fills` entries with different `fill_ts` for the same `exchange_order_id` are the root cause of duplicate `active_positions` rows (different symbol formats + multiple timestamps = multiple AP upserts). Removing the explicit second write eliminates the duplicate fill log entries.

### Secondary Fix: Normalize Pair Format in Active Positions Upsert
**File:** `engine/database.py` — `update_full_snapshot()` and `update_active_positions_snapshot()`
**Action:** Apply `normalize_symbol()` to `symbol` before upserting to `active_positions`

```diff
# In update_full_snapshot (W1) and update_active_positions_snapshot (W2):
-            symbol = row[0]
+            from engine.database import normalize_symbol
+            symbol = normalize_symbol(row[0])
```

**Why:** Even with single exchange_fills entry, if exchange returns `XAUUSDT` and WS returns `XAU/USDC:USDT`, the upsert creates two rows. Normalization ensures one canonical symbol per pair.

## Test Plan
1. Run `reconstruct_offline_fills(dry_run=True)` — verify hash-diff on 7 tables matches
2. Check `exchange_fills` for duplicate `exchange_order_id` with different `fill_ts` — should be 0
3. Check `active_positions` for duplicate `bot_id` + `normalized_pair` + `side` — should be 0
4. Full regression suite pass

## Priority
**HIGH** — Fix 1 (remove double-write) before next engine restart. Fix 2 (normalization) can follow.

---

## Proposed Diff (Primary Fix Only)

```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -1272,25 +1272,6 @@ async def reconcile_all(
             seal_trade_state(bot_id)
             stats['total'] = stats.get('total', 0) + 1
             if order_type in ('tp', 'take_profit', 'exit'):
                 stats['tp_fills'] = stats.get('tp_fills', 0) + 1
             elif order_type in ('entry', 'grid'):
                 stats['entry_fills'] = stats.get('entry_fills', 0) + 1
             else:
                 stats['grid_fills'] = stats.get('grid_fills', 0) + 1
 
-            # Write to exchange_fills audit log
-            from engine.database import record_exchange_fill
-            _pair_row = _credit_cur.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
-            _symbol = _pair_row[0] if _pair_row else 'UNKNOWN'
-            _fill_ts = int(time.time())
-            record_exchange_fill(
-                conn=_credit_conn,
-                exchange_order_id=str(order_id),
-                client_order_id=client_cid or '',
-                symbol=_symbol,
-                side=fill_side,
-                qty=filled_qty,
-                price=avg_price,
-                fill_ts=_fill_ts,
-                source='reconciler-uncredited',
-                bot_id=bot_id,
-                order_type=order_type,
-                step=step,
-                cycle_id=cycle_id,
-            )
-
         # 1.6. 🚀 HISTORY-BASED ORPHAN DETECTION
```