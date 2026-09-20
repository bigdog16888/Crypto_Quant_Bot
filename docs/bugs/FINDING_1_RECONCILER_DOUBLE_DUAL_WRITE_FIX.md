# Track B Finding 1 — Reconciler Double Dual-Write: Proposed Fix

## Summary
`reconciler.py:1254` calls `credit_fill()` which internally writes to `exchange_fills` (dual-write guard at ledger.py:631). Then `reconciler.py:1275-1293` explicitly calls `record_exchange_fill()` again with same `exchange_order_id` but different `fill_ts` (time.time() vs historical). UNIQUE constraint on `(exchange_order_id, fill_ts, qty, price)` does not catch this → duplicate `exchange_fills` entries → double-counted position on restart.

## Evidence (Live in crypto_bot.db)
```
exchange_order_id=350133162: backfill ts=1789084225, reconciler-uncredited ts=1789637285
exchange_order_id=350133201: backfill ts=1789085306, reconciler-uncredited ts=1789637285
```
Bot 10007 (BNB) — active_positions.last_checked=1789647583 (after both). Currently NOT double-counting because checkpoint filters them out. **On next restart, last_checked resets → both counted → -0.02 net qty error.**

## Proposed Fix

### File: engine/reconciler.py:1275-1293
**Action:** Delete the explicit `record_exchange_fill` block (lines 1275-1293). The `credit_fill` internal dual-write is sufficient.

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

## Why This Is Correct
- `credit_fill` already writes to `exchange_fills` via dual-write (ledger.py:607-656)
- The dual-write guard `not (delta <= 0 and is_cumulative)` correctly suppresses replays
- The explicit second write bypasses the guard and uses a different timestamp
- `exchange_fills` is the immutable position source — duplicates = double-counting
- Removing the explicit write preserves the audit trail (credit_fill writes with source='reconciler-uncredited')

## Test Plan
1. Run `reconstruct_offline_fills(dry_run=True)` — verify hash-diff on 7 tables
2. Query `exchange_fills` for duplicate `exchange_order_id` with different `fill_ts` — should be 0
3. Check `active_positions` for BNB (bot 10007) after simulated restart — net qty should be -0.04 (not -0.06)
4. Full regression suite pass

## Priority
**HIGH** — Fix before next engine restart (latent bug that manifests on restart).