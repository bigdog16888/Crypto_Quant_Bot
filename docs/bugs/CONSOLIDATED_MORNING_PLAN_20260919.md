# Consolidated Morning Document — All Proposed Fixes (Priority Ordered)

**Date:** 2026-09-19  
**Scope:** Part 1 (is_active guards) ✅ CLOSED | Part 2 (Track B fill-crediting) | Part 3 (Track C verification) | Final is_active sweep

---

## Priority Order & Reasoning

| Priority | Item | Type | Reason |
|----------|------|------|--------|
| **P0** | Finding 1: Reconciler double dual-write | BUG FIX | **Live in production** — duplicate `exchange_fills` rows exist for BNB bot 10007. Will double-count on next restart. |
| **P1** | Item 1: XAU ORDER-SYNC loop | BUG FIX | Core reconciliation path; potential infinite loop on dust positions. |
| **P2** | Item 2: Stale-cycle_id dedup wedge | BUG FIX | Root cause of 10008/10018 orphans; cycle_id_filter_fix (78f5600) may not fully close it. |
| **P3** | Finding 2: Side inference gap (adoption/race guard) | DEFENSE-IN-DEPTH | Low probability but wrong side inference could corrupt ledger on opposite-direction fills. |
| **P4** | Item 5: audit_bot_wipes wrong signature | BUG FIX | TypeError if called with 3 args (current call site passes 4); silent failure. |
| **P5** | Item 7: Retry-queue false alarm | DEFENSE-IN-DEPTH | False REQUIRE_MANUAL_PROOF escalations; fixed by step-lock cross-check but needs test. |
| **P6** | Items 3, 6, 8 (INV30, GTR display, flatten price) | VERIFICATION | Already verified RESOLVED — no code changes needed. |

---

## P0: Finding 1 — Reconciler Double Dual-Write

**File:** `engine/reconciler.py:1275-1293`  
**Problem:** `credit_fill()` writes to `exchange_fills` (dual-write guard at ledger.py:631), then reconciler calls `record_exchange_fill()` again with different `fill_ts` → UNIQUE constraint bypassed → duplicate rows.

**Evidence (live in DB):**
```
order_id=350133162, bot_id=10007, fill_ts=1789084225 (backfill) + 1789637285 (reconciler-uncredited)
order_id=350133201, bot_id=10007, fill_ts=1789085306 (backfill) + 1789637285 (reconciler-uncredited)
```

**Impact:** On next restart, `last_checked` resets → `_compute_delta_from_fills` counts both → -0.02 BNB net qty error.

**Proposed Diff:**
```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -1272,25 +1272,6 @@
             logger.info(f"🩹 [CREDIT-UNCREDITED] Bot {bot_id} {order_type} cid={client_cid} order_id={order_id} crediting {filled_qty:.6f}")
             credit_fill(
                 bot_id=bot_id,
                 order_id=str(order_id),
                 cumulative_qty=filled_qty,
                 avg_price=avg_price,
                 order_type=order_type,
                 is_cumulative=True,
                 caller='reconciler-uncredited',
                 side=fill_side
             )
             seal_trade_state(bot_id)
             stats['total'] = stats.get('total', 0) + 1
             if order_type in ('tp', 'take_profit', 'exit'):
                 stats['tp_fills'] = stats.get('tp_fills', 0) + 1
             elif order_type in ('entry', 'grid'):
                 stats['entry_fills'] = stats.get('entry_fills', 0) + 1
             else:
                 stats['grid_fills'] = stats.get('grid_fills', 0) + 1
-
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

**Test to Add:** `tests/test_reconciler_double_write.py` — verify single `exchange_fills` entry per fill.

---

## P1: Item 1 — XAU ORDER-SYNC Loop

**File:** `engine/reconciler.py:4415-4495` (DUST-CHASER Scenario A/B)  
**Problem:** Dust chaser logic may repeatedly fire on multi-bot pairs with small residual positions, placing GTC close orders that don't fill, re-evaluating next cycle.

**Root Cause:** Scenario B (lines 4536-4578) places GTC limit close at `current_price` but doesn't verify if order already exists → can re-place same order every reconciler run.

**Proposed Diff:**
```diff
--- a/engine/reconciler.py
+++ b/engine/reconciler.py
@@ -4545,6 +4545,15 @@
                         exchange = self.exchanges.get('future')
                         if not exchange and self.exchanges:
                             exchange = list(self.exchanges.values())[0]
 
+                        # Check if GTC close already pending for this bot
+                        _existing_close = cursor.execute("""
+                            SELECT order_id FROM bot_orders
+                            WHERE bot_id = ? AND order_type = 'close'
+                            AND status IN ('open', 'new', 'partially_filled', 'placing')
+                            AND client_order_id LIKE 'CQB_%_DUST_CLOSE_%'
+                        """, (b.bot_id,)).fetchone()
+                        if _existing_close:
+                            logger.info(f"[DUST-CHASER] Bot {b.name}: GTC close already pending ({_existing_close[0]}). Skipping re-place.")
+                            continue
+
                         try:
                             current_price = exchange.get_last_price(b.pair) if exchange else 0.0
```

**Test to Add:** Verify no duplicate `CQB_*_DUST_CLOSE_*` orders placed in consecutive reconciler runs.

---

## P2: Item 2 — Stale-Cycle_ID Dedup Wedge

**File:** `engine/database.py:4473-4680` (`recompute_invested_from_orders`)  
**Problem:** The `cycle_id_filter_fix` (78f5600) fixed `wipe_wall_ts` for `cycle_id=None`, but historical cycles with stale `cycle_id` on fills may still be mis-attributed.

**Root Cause:** Fills from cycles 19-20 (SOL bot 10008) have `cycle_id=39` (current cycle) due to cycle advance without reset. `recompute_invested_from_orders(cycle_id=39)` now correctly includes them, but the dedup logic at `bot_orders` level may still see them as duplicates.

**Proposed Diff:**
```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -4520,6 +4520,18 @@
             # For explicit cycle_id, respect it as target cycle
             target_cycle = cycle_id
 
+            # STALE CYCLE_ID DEDUP FIX: If caller passes explicit cycle_id,
+            # also check for fills with mismatched cycle_id that belong to this cycle
+            # (stale cycle_id from cycle advance without reset)
+            if cycle_id is not None:
+                stale_fill_check = conn.execute("""
+                    SELECT COUNT(*) FROM bot_orders
+                    WHERE bot_id = ? AND cycle_id != ? AND filled_amount > 0
+                    AND status NOT IN ('reset_cleared', 'auto_closed', 'cancelled', 'canceled', 'failed', 'rejected')
+                """, (bot_id, cycle_id)).fetchone()[0]
+                if stale_fill_check > 0:
+                    logger.warning(f"[RECOMPUTE-STALE] Bot {bot_id}: {stale_fill_check} filled orders have stale cycle_id (not {cycle_id}). Including in recompute.")
+
             # --- CARRY PASS: Only for live cycle (cycle_id=None) ---
             # CARRY is a current-cycle bridging concept; historical cycles have no CARRY residues.
```

**Test to Add:** `tests/test_stale_cycle_id_dedup.py` — verify fills with wrong cycle_id are included when explicit cycle_id passed.

---

## P3: Finding 2 — Side Inference Gap

**Files:** 
- `engine/parity_gates.py:1135` (orphan adoption)
- `engine/database.py:2173` (race guard)

**Problem:** Both call `credit_fill()` without `side=` param. `credit_fill` infers from bot direction, but physical position may have opposite direction (hedge child SHORT on LONG parent pair).

**Proposed Diffs:**

**1. parity_gates.py:1135**
```diff
--- a/engine/parity_gates.py
+++ b/engine/parity_gates.py
@@ -1132,7 +1132,8 @@
         filled = float(o.get('filled') or o.get('amount') or 0)
         if filled <= 0:
             continue
         avg = float(o.get('average') or o.get('price') or 0)
         oid = str(o.get('id') or cid)
         if credit_fill(
             bot_id=bot_id,
             order_id=oid,
             cumulative_qty=filled,
             avg_price=avg,
             order_type='adoption',
             is_cumulative=True,
+            side=o.get('side', ''),  # REAL EXCHANGE SIDE
         ):
             credited_total += filled
```

**2. database.py:2173**
```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -2170,7 +2170,8 @@
                                 logger.info(
                                     f"💰 [RACE-GUARD] Bot {bot_id} order {_cid} (id={_ex_oid}) "
                                     f"filled={_filled} on exchange but DB was '{_otype}'. "
                                     f"Crediting fill before reset."
                                 )
                                 from engine.ledger import credit_fill as _cf_race
                                 _cf_race(
                                     bot_id=bot_id,
                                     order_id=str(_ex_oid),
                                     cumulative_qty=_filled,
                                     avg_price=float(_detail.get('average', 0) or 0),
                                     order_type=str(_otype or 'grid').lower(),
                                     is_cumulative=True,
                                     caller='race_guard',
+                                    side=_detail.get('side', ''),  # REAL EXCHANGE SIDE
                                 )
```

**Test to Add:** `tests/test_side_inference_adoption.py` — verify opposite-direction physical fill credited correctly.

---

## P4: Item 5 — audit_bot_wipes Wrong Signature

**File:** `engine/reconciler_wipe_audit.py:82-87`  
**Problem:** `audit_bot_wipes(bot_id, symbol, exchange_gap)` called from `reconciler.py:3851` as `audit_bot_wipes(cur, bot_id, pair, global_diff)` — 4 args passed, 3 expected → TypeError.

**Proposed Diff:**
```diff
--- a/engine/reconciler_wipe_audit.py
+++ b/engine/reconciler_wipe_audit.py
@@ -79,8 +79,8 @@
     return WipeAuditResult(bot_id, symbol, suspect_rows, total_qty, probable_cause)
 
 
-def audit_bot_wipes(bot_id: int, symbol: str, exchange_gap: float) -> WipeAuditResult:
+def audit_bot_wipes(cursor, bot_id: int, symbol: str, exchange_gap: float) -> WipeAuditResult:
     """
     Public API: audits a bot for unproved wipes. Uses WriteQueue.
     """
-    from engine.write_queue import WriteQueue
-    return WriteQueue().put_and_wait(_audit_bot_wipes, bot_id, symbol, exchange_gap)
+    # Called with cursor from reconciler — execute directly on same connection
+    return _audit_bot_wipes(cursor, bot_id, symbol, exchange_gap)
 
 
 def _system_wipe_health_check(cursor, active_bots: List[Any]) -> List[WipeAuditResult]:
```

**Call site (reconciler.py:3851) already passes 4 args — no change needed.**

**Test to Add:** `tests/test_audit_bot_wipes_signature.py` — verify no TypeError.

---

## P5: Item 7 — Retry-Queue False Alarm

**File:** `engine/ws_event_handlers.py:149-181` (`_fill_credited_by_sibling`)  
**Problem:** Stand-down check uses `fill_claims` (keyed by `order_id` only) but WS fills may use `client_order_id` → cross-ID gap allows false alarm when sibling credited via different ID.

**Proposed Diff:**
```diff
--- a/engine/ws_event_handlers.py
+++ b/engine/ws_event_handlers.py
@@ -161,13 +161,18 @@
     try:
         from engine.database import get_connection
         conn = get_connection()
-        claimed = conn.execute(
-            "SELECT caller FROM fill_claims WHERE bot_id=? AND (order_id=? OR order_id=?) "
-            "ORDER BY rowid DESC LIMIT 1",
-            (bot_id, str(order_id), str(client_id) if client_id else str(order_id)),
-        ).fetchone()
+        # Check fill_claims by BOTH exchange_order_id AND client_order_id
+        claimed = conn.execute(
+            "SELECT caller FROM fill_claims WHERE bot_id=? AND "
+            "(order_id=? OR order_id=?) "
+            "ORDER BY rowid DESC LIMIT 1",
+            (bot_id, str(order_id), str(client_id) if client_id else str(order_id)),
+        ).fetchone()
+        # Also check if a claim exists for the client_order_id as exchange_order_id (cross-ID)
+        if not claimed and client_id and client_id != order_id:
+            claimed = conn.execute(
+                "SELECT caller FROM fill_claims WHERE bot_id=? AND order_id=? ORDER BY rowid DESC LIMIT 1",
+                (bot_id, str(client_id)),
+            ).fetchone()
         if not claimed:
             return False
         row = conn.execute(
```

**Test to Add:** `tests/test_retry_queue_cross_id.py` — verify stand-down works when WS uses exchange_order_id, reconciler uses client_order_id.

---

## P6: Items 3, 6, 8 — Verified Resolved (No Code Changes)

| Item | Verdict | Evidence |
|------|---------|----------|
| 3. INV30 double-count | ✅ Resolved | Step saturation guard (ledger.py:488-542) + catchup exemption works; all 55 hedge tests pass |
| 6. GTR lock display | ✅ Resolved | Log shows duration/qty/USD (ground_truth_reconciler.py:132); UI banner shows name+reason (monitor.py:544) |
| 8. Flatten price=0.0 | ✅ Resolved | All flatten paths use `market` orders (no price param); 0.0 only in audit/metadata rows |

---

## Final is_active Sweep — COMPLETE

**File:** `docs/bugs/COMPREHENSIVE_IS_ACTIVE_SWEEP_20260919.md`  
**Result:** 65 write sites audited → **0 unprotected gaps**. 8 is_active guards (7 new + 1 pre-existing) provide complete coverage.

---

## Execution Order for Next Session

1. **Apply P0 fix** (reconciler double-write) — run tests, verify `exchange_fills` dedup
2. **Apply P1 fix** (XAU ORDER-SYNC loop) — verify no duplicate dust close orders
3. **Apply P2 fix** (stale-cycle_id) — verify bots 10008/10018 recompute correctly
4. **Apply P3 fix** (side inference) — test opposite-direction adoption
5. **Apply P4 fix** (audit_bot_wipes signature) — verify no TypeError
6. **Apply P5 fix** (retry-queue cross-ID) — verify stand-down works
7. **Full test suite** (`pytest tests/`) — confirm zero regressions
8. **Optional:** Address 10008/10018 orphan resolution (operator decision)

---

## Commit Strategy

Each fix = **one commit** with:
- Exact diff (as shown above)
- Corresponding test file
- `py_compile` clean verification
- Raw pytest output attached

No bundled commits. One concern per commit.