"""
engine/runner/cycle_loop.py

Cycle orchestration cluster, extracted byte-for-byte from the original
engine/runner.py (now engine/runner/__init__.py).

Contains:
  - CycleLoopMixin._handle_pending_flatten  (original lines 328-487)
  - CycleLoopMixin._handle_pending_close    (original lines 489-596)
  - CycleLoopMixin.run_cycle                (original lines 597-1297)

Extracted verbatim (no logic changes). These methods are mixed into
BotRunner via multiple inheritance; all `self.<method>()` calls resolve
through the combined MRO, so execution order is identical to the
pre-move single-file version.
"""

import time
import json
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

from engine.database import (
    get_connection,
    update_full_snapshot,
)
from engine.exchange_interface import normalize_symbol, normalize_market_type
from engine.bot_executor import BotExecutor
from engine.metrics import BOT_CYCLE_TIME
from engine.integrity import enforce_integrity
from engine.ws_cache import get_ws_cache

from config.settings import config

import logging
logger = logging.getLogger(__name__)


class CycleLoopMixin:
    def _handle_pending_flatten(self, bot_id, pair, direction, open_qty, conn):
        """
        Execute a netting-aware forced close for a bot in pending_flatten state.
        Follows INV-15: exchange first, DB second.
        Follows INV-16: writes exchange_order_audit WAL receipt before order.

        Uses compute_closeable_qty to determine how much of the virtual position is
        physically backed by live exchange net exposure before placing any reduceOnly
        order. This prevents Binance 400 ReduceOnly rejections caused by One-Way
        Netting when sibling bots hold the opposing net position.
        """
        import time
        from engine.recovery import compute_closeable_qty, resolve_gated_bot
        from engine.parity_gates import get_exchange_signed_net

        logger.warning(
            f"[PENDING-FLATTEN] Bot {bot_id} ({pair}): executing netting-aware forced "
            f"close for open_qty={open_qty:.6f}. Checking live exchange net first."
        )

        ex = self.exchange or (list(self.exchanges.values())[0] if self.exchanges else None)
        if not ex:
            raise RuntimeError("No exchange interface available on runner")

        # Fetch live signed net before any exchange order or DB change
        live_net = get_exchange_signed_net(ex, pair)
        if live_net is None or live_net == 'mock_unconfigured':
            logger.error(
                f"[PENDING-FLATTEN] Bot {bot_id}: fetch_positions failed for {pair}. "
                f"Cannot determine closeable_qty — leaving bot gated."
            )
            return False

        closeable_qty = compute_closeable_qty(direction, open_qty, live_net)
        unphysical_remainder = round(open_qty - closeable_qty, 8)
        fill_price = 0.0

        logger.warning(
            f"[PENDING-FLATTEN] Bot {bot_id}: live_net={live_net:.6f} "
            f"virtual={open_qty:.6f} closeable={closeable_qty:.6f} "
            f"unphysical_remainder={unphysical_remainder:.6f}"
        )

        if closeable_qty > 1e-8:
            # INV-15 Phase 1: Write WAL receipt before exchange call
            cid = f"CQB_{bot_id}_FLATTEN_{int(time.time())}"
            conn.execute("""
                INSERT INTO bot_orders
                (bot_id, order_type, status, amount, filled_amount, price,
                 client_order_id, cycle_id, created_at, updated_at)
                SELECT ?, 'flatten_close', 'placing', ?, 0, 0, ?, cycle_id,
                       ?, ?
                FROM trades WHERE bot_id=?
            """, (bot_id, closeable_qty, cid,
                  int(time.time()), int(time.time()), bot_id))
            conn.commit()

            # INV-15 Phase 2: Execute exchange close for physical portion
            try:
                close_side = 'sell' if direction.upper() == 'LONG' else 'buy'
                is_testnet = bool(
                    getattr(ex, 'is_testnet', False) or
                    getattr(getattr(ex, 'exchange', None), 'sandbox', False)
                )
                params = {'reduceOnly': True}
                if is_testnet:
                    params['positionSide'] = 'BOTH'
                else:
                    params.pop('positionSide', None)
                params['newClientOrderId'] = cid

                order = ex.create_order(
                    pair, 'market', close_side, closeable_qty, params=params
                )
                filled_qty = float(order.get('filled') or closeable_qty)
                fill_price = float(order.get('average') or order.get('price') or 0)

                conn.execute("""
                    UPDATE bot_orders SET status='filled', filled_amount=?,
                    price=?, order_id=?, updated_at=?
                    WHERE client_order_id=? AND bot_id=?
                """, (filled_qty, fill_price, str(order.get('id', '')),
                      int(time.time()), cid, bot_id))
                conn.commit()
                logger.warning(
                    f"[PENDING-FLATTEN] Bot {bot_id}: exchange close confirmed "
                    f"{filled_qty:.6f} @ {fill_price:.4f}"
                )

            except Exception as e:
                conn.execute("""
                    UPDATE bot_orders SET status='failed',
                    notes=?, updated_at=? WHERE client_order_id=? AND bot_id=?
                """, (f"flatten failed: {e}", int(time.time()), cid, bot_id))
                conn.execute("""
                    UPDATE bots SET status='REQUIRE_MANUAL_PROOF',
                    cascade_started_at=? WHERE id=?
                """, (int(time.time()), bot_id))
                conn.commit()
                logger.error(
                    f"[PENDING-FLATTEN] Bot {bot_id}: exchange close FAILED: {e}. "
                    f"Set REQUIRE_MANUAL_PROOF."
                )
                return False
        else:
            logger.info(
                f"[PENDING-FLATTEN] Bot {bot_id}: closeable_qty=0 — virtual position "
                f"is unphysical (net on wrong side). Routing to direct unphysical wipe."
            )

        # After exchange order (or skipped), wipe full virtual DB position via safe_wipe_bot.
        # safe_wipe_bot Guard 2.0 will live-verify exchange flat for this direction.
        try:
            from engine.database import safe_wipe_bot
            wipe_ok = safe_wipe_bot(
                bot_id=bot_id,
                pair=pair,
                direction=direction,
                exit_price=fill_price,
                reason=(
                    f"pending_flatten: closeable={closeable_qty:.6f} "
                    f"unphysical={unphysical_remainder:.6f}"
                ),
                bypass_ledger_guard=True,
                human_approved=True,
            )

            if wipe_ok:
                logger.warning(
                    f"[PENDING-FLATTEN] Bot {bot_id}: fully resolved. "
                    f"closeable={closeable_qty:.6f} "
                    f"unphysical={unphysical_remainder:.6f}"
                )
                return True
            else:
                # safe_wipe_bot refused (Guard 2.0 found residual position) — stay gated
                conn.execute("""
                    UPDATE bots SET status='REQUIRE_MANUAL_PROOF',
                    cascade_started_at=? WHERE id=?
                """, (int(time.time()), bot_id))
                conn.commit()
                logger.error(
                    f"[PENDING-FLATTEN] Bot {bot_id}: safe_wipe_bot refused after close. "
                    f"Set REQUIRE_MANUAL_PROOF."
                )
                return False

        except Exception as e_wipe:
            conn.execute("""
                UPDATE bots SET status='REQUIRE_MANUAL_PROOF',
                cascade_started_at=? WHERE id=?
            """, (int(time.time()), bot_id))
            conn.commit()
            logger.error(
                f"[PENDING-FLATTEN] Bot {bot_id}: safe_wipe_bot raised: "
                f"{e_wipe}. Set REQUIRE_MANUAL_PROOF."
            )
            return False



    def _handle_pending_close(self, bot_id: int, bot_name: str, pair: str, direction: str) -> None:
        """
        Called every cycle while a bot is in status='pending_close'.

        Checks whether the flatten_close market order has been filled (via the
        WS-credited filled_amount in bot_orders, or a REST poll).  Once confirmed,
        calls safe_wipe_bot without force so the physical guard naturally passes
        (the exchange will be flat).  After 3 cycles without fill confirmation
        a warning is logged but no automated escalation is performed.
        """
        cycle_n = self.pending_close_cycles.get(bot_id, 0)
        MAX_WAIT_CYCLES = 3

        conn = get_connection()
        # Find the most recent flatten_close order for this bot
        row = conn.execute("""
            SELECT id, order_id, client_order_id, filled_amount, status, price
            FROM bot_orders
            WHERE bot_id=? AND order_type='flatten_close'
            ORDER BY id DESC LIMIT 1
        """, (bot_id,)).fetchone()

        if not row:
            logger.warning(
                f"[HEDGE-FLATTEN-POLL] Bot {bot_name} is pending_close but no flatten_close "
                f"order found in bot_orders. Will retry next cycle."
            )
            self.pending_close_cycles[bot_id] = cycle_n + 1
            return

        db_row_id, exchange_oid, cid, filled_amount, order_status, fill_price = row
        filled_amount = float(filled_amount or 0)
        fill_price = float(fill_price or 0)

        # ── Check fill via WS-credited amount ────────────────────────────────
        is_filled = filled_amount > 0 or str(order_status).lower() in ('filled', 'closed')

        # ── Fallback: poll the exchange directly ──────────────────────────────
        if not is_filled and exchange_oid:
            try:
                ex = list(self.exchanges.values())[0] if self.exchanges else None
                if ex:
                    closed = ex.fetch_closed_orders(pair, limit=20)
                    for o in (closed or []):
                        if (str(o.get('id')) == str(exchange_oid) or
                                o.get('clientOrderId', '') == cid):
                            if float(o.get('amount', 0)) > 0:
                                is_filled = True
                                fill_price = float(o.get('average') or o.get('price') or 0.0)
                                # Update the DB row so future cycles don't re-poll
                                conn.execute(
                                    "UPDATE bot_orders SET price=?, filled_amount=?, status='filled', updated_at=? WHERE id=?",
                                    (fill_price, float(o.get('amount', 0)), int(time.time()), db_row_id)
                                )
                                conn.commit()
                                break
            except Exception as _poll_err:
                logger.warning(f"  ⚠️ [HEDGE-FLATTEN-POLL] REST poll failed (non-fatal): {_poll_err}")

        # If it filled but we don't have the price, try to fetch it
        if is_filled and fill_price <= 0.0 and exchange_oid:
            try:
                ex = list(self.exchanges.values())[0] if self.exchanges else None
                if ex:
                    o = ex.fetch_order(exchange_oid, pair)
                    fill_price = float(o.get('average') or o.get('price') or 0.0)
            except Exception as _pe:
                logger.warning(f"  ⚠️ [HEDGE-FLATTEN-POLL] Price fetch failed (non-fatal): {_pe}")

        if is_filled:
            logger.info(
                f"✅ [HEDGE-FLATTEN-POLL] Bot {bot_name} flatten_close confirmed filled "
                f"(cid={cid}) @ {fill_price:.4f}. Calling safe_wipe_bot..."
            )
            try:
                from engine.database import safe_wipe_bot
                wiped = safe_wipe_bot(
                    bot_id=bot_id, pair=pair, direction=direction,
                    exit_price=fill_price,
                    reason="HEDGE-FLATTEN: market close confirmed filled",
                    human_approved=True
                )
                if wiped:
                    self.pending_close_cycles.pop(bot_id, None)
                    logger.info(f"✅ [HEDGE-FLATTEN-POLL] Bot {bot_name} wiped cleanly after fill confirmation.")
                else:
                    logger.warning(
                        f"⚠️ [HEDGE-FLATTEN-POLL] safe_wipe_bot blocked for {bot_name} even after fill. "
                        f"Physical guard fired — exchange may not be fully flat yet. Will retry next cycle."
                    )
                    self.pending_close_cycles[bot_id] = cycle_n + 1
            except Exception as _we:
                logger.error(f"❌ [HEDGE-FLATTEN-POLL] safe_wipe_bot raised: {_we}")
                self.pending_close_cycles[bot_id] = cycle_n + 1
        else:
            self.pending_close_cycles[bot_id] = cycle_n + 1
            if cycle_n + 1 >= MAX_WAIT_CYCLES:
                logger.warning(
                    f"⚠️ [HEDGE-FLATTEN-POLL] Bot {bot_name} flatten_close order still UNFILLED "
                    f"after {cycle_n + 1} cycles (cid={cid}). "
                    f"Not escalating automatically — manual review recommended."
                )
            else:
                logger.info(
                    f"[HEDGE-FLATTEN-POLL] Bot {bot_name} flatten_close not yet filled "
                    f"(cycle {cycle_n + 1}/{MAX_WAIT_CYCLES}). Will check next cycle."
                )

    def run_cycle(self):

        # 🚨 Freshness Check: Exit if code on disk is newer than runner memory (structural fix for stale processes)
        if not config.TESTING_MODE:
            try:
                from scripts.verify_deployment import get_newest_modified_time
                newest_time, newest_file = get_newest_modified_time(["engine", "scripts", "ui", "config"])
                if newest_time > self.started_at + 2.0:
                    logger.critical(
                        f"🛑 [DEPLOY-OUTDATED] Code on disk ({newest_file}) was modified "
                        f"after the runner process started. Force-terminating stale process."
                    )
                    self.running = False
                    try:
                        from engine.write_queue import WriteQueue
                        WriteQueue().flush()
                    except Exception as _flush_err:
                        logger.error(f"Failed to flush WriteQueue before exit: {_flush_err}")
                    import sys
                    sys.exit(1)
            except Exception as _fresh_err:
                logger.warning(f"Process freshness check failed: {_fresh_err}")

        start_time = time.time()
        logger.debug("Entering run_cycle")
        if self._abort_if_stop_requested("run-cycle"):
            return False
        self.orders_this_cycle = 0
        self.cycle_count += 1

        # 🚨 Continuous Fill Audit: Runs every cycle to check for active bots with TP or Grid orders
        # that filled on the exchange but were missed by the WebSocket.
        if self._reconciler:
            try:
                self._reconciler._audit_pending_exits()
                self._reconciler._audit_pending_grids()
            except Exception as _audit_err:
                logger.warning(f"Continuous fill audit failed (non-fatal): {_audit_err}")

        # 🛡️ PERIODIC OFFLINE FILL DETECTION (every 10 cycles ≈ every 5 min)
        # Safety net for Demo WS which can silently miss fill events.
        # 🚀 ROOT CAUSE FIX: Use 24h window every 50th cycle (≈25 min) to catch fills
        # that happened more than 2h ago — these were permanently missed by the rolling 2h scan.
        if self.cycle_count % 10 == 0 and self._reconciler:
            try:
                # Use 24h window once per hour (every 50 cycles) to catch old fills
                scan_hours = 24 if self.cycle_count % 50 == 0 else 2
                logger.info(f"[PERIODIC] Running offline fill scan ({scan_hours}h window, cycle {self.cycle_count})...")
                _pof_stats = self._reconciler.reconstruct_offline_fills(since_hours=scan_hours)
                if _pof_stats.get('total', 0) > 0:
                    logger.info(f"✅ [PERIODIC] Offline fills credited: {_pof_stats}")
                else:
                    logger.info(f"✅ [PERIODIC] No new fills found in scan.")
            except Exception as _pof_err:
                logger.warning(f"Periodic offline fill scan failed (non-fatal): {_pof_err}")

        # 🔧 PERIODIC LEDGER ALIGNMENT (every 180 cycles = exactly 15 min at 5s/cycle)
        # WARNING: Do NOT reduce this below 120. _align_memory_to_ledger reads active_positions
        # (refreshed at cycle%60=5min) and relies on seal_trade_state having committed.
        # Running this faster than the snapshot refresh rate causes false SYSTEM_WIPEs.
        if self.cycle_count % 180 == 0 and self._reconciler:
            try:
                logger.info(f"[PERIODIC] Running memory-to-ledger alignment (cycle {self.cycle_count})...")
                self._reconciler._align_memory_to_ledger()
                logger.info("✅ [PERIODIC] Memory-to-ledger alignment complete.")
            except Exception as _mal_err:
                logger.warning(f"Periodic memory-to-ledger alignment failed (non-fatal): {_mal_err}")

        # 🔬 PERIODIC BIDIRECTIONAL PROOF RECONCILIATION (every 60 cycles ≈ every 30 min)
        # Runs adopt_from_physical_positions() to:
        #   PASS 0: Auto-reset bots whose position was externally closed (exchange=0, DB=open)
        #   PASS 1: Verify existing bot_orders fills against exchange reality (heal fill amounts)
        #   PASS 2: Scan exchange fill history for DNA-matching fills not yet in ledger (adopt carry-overs)
        # This runs continuously so gaps are auto-healed without requiring engine restarts.
        if self.cycle_count % 60 == 0 and self._reconciler:
            try:
                logger.info(f"[PERIODIC] Running bidirectional proof reconciliation (cycle {self.cycle_count})...")
                _adopt_results = self._reconciler.adopt_from_physical_positions()
                resets    = sum(1 for r in _adopt_results.values() if r.get('action') == 'auto_reset')
                p1_healed = sum(r.get('p1_healed', 0) for r in _adopt_results.values())
                p2_adopted = sum(r.get('p2_adopted', 0) for r in _adopt_results.values())
                logger.info(
                    f"✅ [PERIODIC] Proof reconciliation: {resets} auto-resets, "
                    f"{p1_healed} P1-healed, {p2_adopted} P2-adopted."
                )
            except Exception as _adopt_err:
                logger.warning(f"Periodic bidirectional proof reconciliation failed (non-fatal): {_adopt_err}")

        # 📸 PERIODIC SNAPSHOT REFRESH (every 60 cycles ≈ every 30 min)
        # Re-primes the WS cache with a fresh exchange position snapshot.
        # Keeps UI dashboard positions current without needing full restart.
        if self.cycle_count % 60 == 0 and self._reconciler:
            try:
                logger.debug(f"[PERIODIC] Refreshing exchange position snapshot (cycle {self.cycle_count})...")
                self._reconciler.prime_startup_snapshot()
                logger.debug("✅ [PERIODIC] Exchange snapshot refreshed successfully.")
            except Exception as _snap_err:
                logger.warning(f"Periodic snapshot refresh failed (non-fatal): {_snap_err}")



        # 🚀 [WS-HEALTH-CHECK] Ensure real-time stream is active
        self._ws_health_check()

        # 1. Global Optimization: Fetch Snapshots once per cycle
        # This fills the ExchangeInterface internal generic cache
        exchange_snapshot = {}
        bots = []
        try:
            logger.debug("Cycle Start - Fetching Bots")
            all_bots = self.get_active_bots()
            bots = [b for b in all_bots if b[9] == 1] # Filter for active bots

            # Handle pending_flatten bots
            conn = get_connection()
            flatten_bots = conn.execute("""
                SELECT b.id, b.pair, b.direction, t.open_qty
                FROM bots b JOIN trades t ON t.bot_id=b.id
                WHERE b.status='pending_flatten' AND b.is_active=1
            """).fetchall()

            flatten_intercepted_ids = set()
            for fb_id, fb_pair, fb_dir, fb_qty in flatten_bots:
                flatten_intercepted_ids.add(fb_id)
                if fb_qty > 0.0001:
                    self._handle_pending_flatten(fb_id, fb_pair, fb_dir, fb_qty, conn)
                else:
                    # Already flat — just reset status
                    conn.execute(
                        "UPDATE bots SET status='Scanning', cascade_started_at=0 "
                        "WHERE id=?", (fb_id,)
                    )
                    conn.commit()

            # Exclude pending_flatten bots from normal cycle execution
            bots = [b for b in bots if b[0] not in flatten_intercepted_ids]

            active_market_types = set()
            for bot in bots:
                config_json = bot[5]
                cfg = json.loads(config_json) if config_json else {}
                mt = normalize_market_type(cfg.get('market_type', config.MARKET_TYPE))
                active_market_types.add(mt)

            if not active_market_types: active_market_types.add(config.MARKET_TYPE)

            # 🚀 Initializing snapshot variables to prevent UnboundLocalError
            snap_pos = None
            snap_bal = None
            snap_orders = None

            for mt in active_market_types:
                if mt in self.exchanges:
                    ex = self.exchanges[mt]

                    # 🚀 FAST-PATH: Use WebSocket Memory Cache if fresh
                    ws_cache = get_ws_cache()

                    if ws_cache.is_fresh(max_age_seconds=15):
                        logger.debug(f"⚡ [WS-CACHE] Reading positions and orders from memory for {mt}")
                        snap_pos = ws_cache.get_all_positions()
                        snap_orders = ws_cache.get_all_open_orders()
                    else:
                        snap_pos = ex.fetch_positions()

                        # 🚀 BUG FIX: Binance Demo FAPI truncates fetch_open_orders() without symbol to ~12 orders!
                        # We must fetch open orders explicitly for every active pair on this market type.
                        # 🔥 OPTIMIZATION: Do this in parallel to prevent API latency from crashing the engine loop!
                        snap_orders = []
                        mt_active_pairs = set([b[2] for b in bots if b[5] and normalize_market_type(json.loads(b[5]).get('market_type', config.MARKET_TYPE)) == mt])
                        if not mt_active_pairs: mt_active_pairs = set([b[2] for b in bots]) # Fallback

                        def _fetch_pair_orders(pair_symbol):
                            try:
                                return ex.fetch_open_orders(pair_symbol)
                            except Exception:
                                return []

                        with ThreadPoolExecutor(max_workers=5) as executor:
                            for pair_orders in executor.map(_fetch_pair_orders, mt_active_pairs):
                                if pair_orders: snap_orders.extend(pair_orders)

                        # 🚀 PRE-POPULATE WS CACHE to avoid data loss on startup
                        if snap_pos is not None and snap_orders is not None:
                            ws_cache.populate_from_rest(snap_pos, snap_orders)

                    # Skip fetch_balance — circuit breaker is disabled, no consumer
                    snap_bal = None

                    # Position Fetch Trace
                    if snap_pos is not None:
                        logger.debug(f"{mt} fetch_positions returned {len(snap_pos)} items: {[p.get('symbol', 'UNK') for p in snap_pos]}")
                    else:
                        logger.debug(f"{mt} fetch_positions returned EMPTY/NONE")

                    # 🚀 FUNDAMENTAL FIX: Handle Fetch Failures Explicitly

                    if snap_pos is None:
                        logger.warning(f"⚠️ [SNAPSHOT-FAIL] Failed to fetch positions for {mt}. Skipping cycle.")
                        return 5.0 # Short sleep, retry next cycle

                    # BUG FIX #6 (FUNDAMENTAL): Safety Gate for Empty Snapshots
                    # Only calculate mismatch if we confirm fetch was SUCCESSFUL (snap_pos is not None)
                    # If Exchange returns 0 positions (snap_pos == []) but DB expects trades:
                    if len(snap_pos) == 0:
                        expected_count = self.get_expected_active_positions_count()
                        if expected_count > 0:
                            logger.warning(f"⚠️ [SNAPSHOT-CHECK] DB expects {expected_count} positions but Exchange returned 0. Retrying with FORCE REFRESH...")
                            time.sleep(1.0) # Short wait
                            snap_pos = ex.fetch_positions()

                            if snap_pos is None: # Check if refresh also failed
                                logger.warning(f"⚠️ [SNAPSHOT-FAIL] Force Refresh also failed. Skipping cycle.")
                                return 5.0

                            if len(snap_pos) == 0:
                                logger.warning(f"⚠️ [SNAPSHOT-ZERO] Confirmed: DB expects {expected_count} positions but Exchange returned 0.")
                                logger.warning(f"🔄 Allowing cycle to continue — ghost-bust will reconcile via net-sum check.")
                            else:
                                logger.info(f"✅ [SNAPSHOT-RECOVERY] Force Refresh successful: Found {len(snap_pos)} positions.")

                    logger.debug(f"DEBUG: Processing {len(snap_pos)} positions for {mt}")

                    # 🚀 FUNDAMENTAL FIX: Ensure we actually populate the snapshot dict!
                    exchange_snapshot[mt] = {
                        'positions': snap_pos,
                        'orders': snap_orders,
                        'balance': snap_bal
                    }

                    # ── [v3.3.1] PRE-SNAPSHOT LEDGER SEAL ──────────────────────────
                    # Seal all active bot ledgers from confirmed bot_orders fills BEFORE
                    # writing the active_positions snapshot. Without this seal, a WS fill
                    # event that committed to bot_orders in a previous cycle but whose
                    # sync_trades_from_orders propagation hadn't landed yet would leave
                    # trades.total_invested stale. SNAP-ALLOCATE (now proof-based since
                    # v3.3.1) is immune, but sealing here keeps trades in lockstep with
                    # bot_orders for every other consumer (bot_executor, circuit breaker,
                    # UI PnL display) and eliminates any residual staleness window.
                    # Overhead: O(N_active_bots) lightweight SQL reads — well within 5s budget.
                    try:
                        from engine.database import sync_trades_from_orders as _sts
                        _seal_corrected = 0
                        for _sb in bots:
                            try:
                                if _sts(_sb[0]):
                                    _seal_corrected += 1
                            except Exception as _ste:
                                logger.debug(f"[PRE-SNAP-SEAL] Bot {_sb[0]} seal skipped: {_ste}")
                        if _seal_corrected:
                            logger.info(f"[PRE-SNAP-SEAL] Sealed {_seal_corrected} bot ledger(s) before snapshot.")
                    except Exception as _seal_ex:
                        logger.warning(f"⚠️ [PRE-SNAP-SEAL] Seal loop failed (non-fatal): {_seal_ex}")
                    # ────────────────────────────────────────────────────────────────

                    # Fix 4: Write active_positions snapshot EVERY cycle so UI always has fresh data
                    try:
                        from engine.database import update_active_positions_snapshot
                        update_active_positions_snapshot(snap_pos)
                    except Exception as _snap_ex:
                        logger.warning(f"⚠️ [active_positions] Failed to write snapshot: {_snap_ex}")

                    # POSITION MONITORING: Throttle to every 10 cycles (~50s)
                    # FLAG-ONLY: No state mutations. Reconciler handles all decisions with evidence.
                    if self.cycle_count % 10 != 0:
                        pass  # Skip position monitoring this cycle
                    else:
                        # --- FLAG-ONLY POSITION MONITORING ---
                        # Log mismatches for visibility but do NOT fabricate trade records
                        # or reset bots. The evidence-based reconciler handles all corrections.
                        checked_pairs = set()
                        for pos in snap_pos:
                            pos_symbol = pos['symbol']
                            pos_amt = pos['contracts']
                            if pos_amt == 0: continue

                            pos_side_real = 'LONG' if pos_amt > 0 else 'SHORT'
                            entry_price = float(pos['entryPrice'])
                            full_exch_notional = abs(float(pos_amt)) * entry_price

                            relevant_bots = [b for b in bots if normalize_symbol(b[2]) == normalize_symbol(pos_symbol)]
                            same_dir_bots = [b for b in relevant_bots if b[3].upper() == pos_side_real]

                            # Check if any bot claims this position (Threshold lowered to $0.01 for cent-level accuracy)
                            claimed = any(float(b[6] or 0) > 0.01 for b in same_dir_bots)
                            if not claimed and full_exch_notional > 0.01:
                                logger.info(f"📋 [MONITOR] Unclaimed {pos_side_real} position on {pos_symbol}: ${full_exch_notional:.2f} @ {entry_price}. Reconciler will handle.")

                        # Flag bots that think they're invested but exchange disagrees
                        relevant_bots_for_mt = [b for b in bots if b[5] and normalize_market_type(json.loads(b[5]).get('market_type', config.MARKET_TYPE)) == mt]
                        if not relevant_bots_for_mt:
                             relevant_bots_for_mt = [b for b in bots if config.MARKET_TYPE == mt]

                        for bot in relevant_bots_for_mt:
                            # Use index access — safe regardless of how many columns get_active_bots() returns
                            b_id      = bot[0]
                            b_name    = bot[1]
                            b_pair    = bot[2]
                            b_direction = bot[3]
                            b_invested = float(bot[6] or 0)  # col 6 = total_invested
                            if b_invested <= 0:
                                continue

                            # Check if exchange has any position for this pair
                            found_pos = None
                            for p in snap_pos:
                                if normalize_symbol(p['symbol']) == normalize_symbol(b_pair):
                                    found_pos = p
                                    break

                            if not found_pos or float(found_pos['contracts']) == 0:
                                logger.warning(f"⚠️ [FLAG-ONLY] Bot {b_name} thinks it has ${b_invested} but exchange has 0 for {b_pair}. Reconciler will handle.")



                    # 🚀 NEW: Pre-fetch OHLCV (Price Data) for all active pairs to feed strategies
                    market_data = {}
                    multi_tf_data = {}  # { pair: { "15m": df, "1h": df, ... } }
                    active_pairs = set([b[2] for b in bots])

                    # TTL mapping: how many seconds before each TF's cache expires
                    _TF_TTL = {
                        '1m': 55, '5m': 280, '15m': 840, '30m': 1700,
                        '1h': 3500, '4h': 14000, '1d': 82800
                    }

                    def _fetch_all_tfs_for_pair(p):
                        try:
                            norm_p = p
                            if '/' not in norm_p:
                                if 'USDC' in norm_p: norm_p = norm_p.replace('USDC', '/USDC')
                                elif 'USDT' in norm_p: norm_p = norm_p.replace('USDT', '/USDT')

                            now_t = time.time()
                            c_key_1m = (p, '1m')
                            _cached_1m = self._tf_cache.get(c_key_1m)

                            # Cache 1m timeframe for 25 seconds to drastically cut REST API pings
                            if _cached_1m and (now_t - _cached_1m['fetched_at']) < 25:
                                p_df = _cached_1m['data']
                            else:
                                p_ohlcv = ex.fetch_ohlcv(norm_p, timeframe='1m', limit=50)
                                p_df = pd.DataFrame(p_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                self._tf_cache[c_key_1m] = {'data': p_df, 'fetched_at': now_t}

                            needed = set()
                            for b_bot in bots:
                                if b_bot[2] == p and b_bot[5]:
                                    c_cfg = json.loads(b_bot[5])
                                    for key in ['cci_tf', 'rsi_tf', 'boll_tf', 'stoch_tf', 'pat_1_tf', 'pat_2_tf', 'pat_3_tf', 'pat_4_tf', 'MTF_Timeframe', 'ATR_Timeframe', 'ATRTimeframe', 'atr_tf']:
                                        if c_cfg.get(key) and c_cfg.get(key) != '1m':
                                            needed.add(c_cfg.get(key))

                            p_tf_d = {'1m': p_df}
                            for tf_val in needed:
                                c_key = (p, tf_val)
                                m_ttl = _TF_TTL.get(tf_val, 300)
                                _cached = self._tf_cache.get(c_key)

                                if _cached and (now_t - _cached['fetched_at']) < m_ttl:
                                    p_tf_d[tf_val] = _cached['data']
                                else:
                                    try:
                                        t_ohlcv = ex.fetch_ohlcv(norm_p, timeframe=tf_val, limit=100)
                                        t_df = pd.DataFrame(t_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                        p_tf_d[tf_val] = t_df
                                        self._tf_cache[c_key] = {'data': t_df, 'fetched_at': now_t}
                                    except Exception as tf_err:
                                        if _cached: p_tf_d[tf_val] = _cached['data']

                            return p, p_df, p_tf_d
                        except Exception as e:
                            logger.error(f"Failed to fetch market data for {p}: {e}")
                            return p, None, None

                    with ThreadPoolExecutor(max_workers=6) as tp_executor:
                        for p_res, p_df_res, p_tf_d_res in tp_executor.map(_fetch_all_tfs_for_pair, active_pairs):
                            if p_df_res is not None:
                                market_data[p_res] = p_df_res
                                multi_tf_data[p_res] = p_tf_d_res

                    # 🚀 UI PERFORMANCE BATCHING: Save the OHLCV Cache to JSON for the Dashboard
                    try:
                        cache_dir = os.path.join(config.ROOT_DIR, 'data')
                        os.makedirs(cache_dir, exist_ok=True)
                        cache_file = os.path.join(cache_dir, 'market_cache.json')
                        tmp_cache_file = os.path.join(cache_dir, 'market_cache_tmp.json')

                        # We need to convert Pandas DataFrames into simple JSON dicts
                        json_ready_cache = {}
                        for pair, tf_dict in multi_tf_data.items():
                            json_ready_cache[pair] = {}
                            for tf, df in tf_dict.items():
                                json_ready_cache[pair][tf] = df.to_dict(orient='records')

                        # ATOMIC WRITE: Write to tmp file, then atomic rename
                        with open(tmp_cache_file, 'w') as f:
                            json.dump(json_ready_cache, f)
                        os.replace(tmp_cache_file, cache_file)
                    except Exception as cache_err:
                        logger.warning(f"Failed to save market cache for UI: {cache_err}")


                    exchange_snapshot[mt] = {
                        'positions': snap_pos,
                        'balance': snap_bal,
                        'open_orders': snap_orders,
                        'market_data': market_data,  # 🚀 1m price data
                        'multi_tf_data': multi_tf_data  # 🚀 All timeframes (TTL-cached)
                    }
        except Exception as e:
            logger.warning(f"Failed to pre-fetch cycle snapshot: {e}")

        # 🚀 FUNDAMENTAL FIX: Active Positions are now updated atomically in 'update_full_snapshot' below.
        # We removed the redundant call to 'update_active_positions_snapshot' here to prevent transaction races.

        # 2. Safety Checks (using snapshots)
        # DISABLE CIRCUIT BREAKER FOR DEBUGGING (False Positives on Testnet)
        # self.check_circuit_breaker(exchange_snapshot=exchange_snapshot)

        # Signal file checks are handled in the main while loop in __main__.
        # Keeping a lightweight in-cycle check here as a secondary safety net.
        from engine.shutdown_control import is_stop_requested
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]) or is_stop_requested():
            return False  # Main loop will handle the file cleanup and liquidation

        # 3. Process Bots
        # ================================================================
        # 🛑 FORCE ENGINE SL INTERCEPT
        # Before processing any bot, check if the UI flagged it with
        # status='stop_loss_triggered'. If so: cancel all its open orders,
        # fire a reduce-only market close, and reset it to idle.
        # This makes the "Force Engine SL" button in Bot Manager actually work.
        # ================================================================
        _sl_conn = get_connection()
        _sl_cur = _sl_conn.cursor()
        _sl_cur.execute("SELECT id, name, pair, direction FROM bots WHERE status='stop_loss_triggered' AND is_active=1")
        sl_flagged_bots = _sl_cur.fetchall()
        pass # _sl_conn.close() disabled for singleton safety

        for sl_bid, sl_name, sl_pair, sl_dir in sl_flagged_bots:
            logger.critical(f"🛑 [FORCE-SL] Bot {sl_name} (ID {sl_bid}) flagged for forced stop. Executing safe close.")
            try:
                from engine.database import safe_wipe_bot
                ex_sl = list(self.exchanges.values())[0] if self.exchanges else None
                if ex_sl:
                    # Cancel all open CQB_ orders for this bot first
                    try:
                        open_ords = ex_sl.fetch_open_orders(sl_pair)
                        for o in (open_ords or []):
                            cid = o.get('clientOrderId', '')
                            if cid.startswith(f'CQB_{sl_bid}_'):
                                ex_sl.cancel_order(o['id'], sl_pair)
                                logger.info(f"  ✅ [FORCE-SL] Cancelled order {cid}")
                    except Exception as _co_err:
                        logger.warning(f"  ⚠️ [FORCE-SL] Could not cancel orders for {sl_name}: {_co_err}")
                    # Fire market reduce-only close natively matching a TP sequence
                    try:
                        exit_side = 'buy' if sl_dir.upper() == 'SHORT' else 'sell'
                        _sl_conn2 = get_connection()
                        qty_row = _sl_conn2.execute(
                            "SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=?", (sl_bid,)
                        ).fetchone()
                        _sl_conn2.close()

                        api_success = False
                        if qty_row and qty_row[0] and qty_row[1] and float(qty_row[1]) > 0:
                            close_qty = float(qty_row[0]) / float(qty_row[1])

                            # 🚀 ROOT CAUSE FIX: Use the native exact tracking ID so WS handles the math.
                            client_order_id = f"CQB_{sl_bid}_TP_MARKETSL{int(time.time())}"

                            ex_sl.create_order(sl_pair, 'market', exit_side, close_qty,
                                               params={'reduceOnly': True, 'clientOrderId': client_order_id})
                            logger.info(f"  ✅ [FORCE-SL] Market close placed via ID {client_order_id}: {exit_side} {close_qty:.6f} {sl_pair}")
                            api_success = True

                    except Exception as _mc_err:
                        logger.warning(f"  ⚠️ [FORCE-SL] Market close rejected by exchange!: {_mc_err}")

                # 🚀 ROOT CAUSE FIX: NEVER wipe the bot manually bypassing proof.
                # If API passed, we set status to pending_sl so the WS catches the fill and executes normal TP shutdown.
                # Record audit but don't freeze the bot permanently
                _r_conn = get_connection()
                if api_success:
                    _r_conn.execute("UPDATE bots SET status='pending_sl' WHERE id=?", (sl_bid,))
                    logger.info(f"  ⏳ [FORCE-SL] Bot {sl_name} pending WS confirmation to formally close.")
                else:
                    # API failed (e.g. 0 qty locally, rejected order, etc).
                    # Do NOT blindly wipe the bot, as this creates Ghost positions if the DB is desynced.
                    logger.warning(f"  ⚠️ [FORCE-SL] Market close API failed or bypassed for {sl_name}. Reverting to normal state without forcing wiping.")
                    # If the bot has a ledger position, it remains IN TRADE. If flat, it scans.
                    qty_row = _r_conn.execute("SELECT total_invested FROM trades WHERE bot_id=?", (sl_bid,)).fetchone()
                    if qty_row and qty_row[0] and float(qty_row[0]) > 0:
                        _r_conn.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (sl_bid,))
                    else:
                        _r_conn.execute("UPDATE bots SET status='Scanning' WHERE id=?", (sl_bid,))

                _r_conn.commit()
                pass # _r_conn.close() disabled for singleton safety

            except Exception as _sl_err:
                logger.error(f"❌ [FORCE-SL] Failed to process forced SL for {sl_name}: {_sl_err}")

        # Remove SL-flagged bots from this cycle's run list so they don't also get processed normally
        sl_flagged_ids = {b[0] for b in sl_flagged_bots}
        bots = [b for b in bots if b[0] not in sl_flagged_ids]

        # ================================================================
        # 🛑 ASYNC FLATTEN INTERCEPT  (v3.5.0)
        # Bots already pending_close   → poll for fill confirmation.
        # Both are removed from the normal process_bot run list.
        # ================================================================
        flatten_intercepted_ids = set()
        try:
            _f_conn = get_connection()
            _f_cur = _f_conn.cursor()
            _f_cur.execute(
                "SELECT id, name, pair, direction FROM bots "
                "WHERE status IN ('pending_close') AND is_active=1"
            )
            flatten_flagged = _f_cur.fetchall()
        except Exception as _ff_err:
            logger.warning(f"[ASYNC-FLATTEN] Could not query flatten-flagged bots: {_ff_err}")
            flatten_flagged = []

        for f_bid, f_name, f_pair, f_dir in flatten_flagged:
            flatten_intercepted_ids.add(f_bid)
            try:
                self._handle_pending_close(f_bid, f_name, f_pair, f_dir)
            except Exception as _hpc_err:
                logger.error(f"[ASYNC-FLATTEN] _handle_pending_close raised for {f_name}: {_hpc_err}")

        # Remove flatten-intercepted bots and pending_hedge_close bots so they skip normal strategy execution
        SKIP_STATUSES = {'pending_hedge_close'}
        bots = [b for b in bots if b[0] not in flatten_intercepted_ids and b[12] not in SKIP_STATUSES]

        # ================================================================
        # 🔁 v2.0 TP CASCADE DRAIN

        # WS handler cannot cancel exchange orders (no exchange obj).
        # It registers (bot_id, pair, exit_price) in ledger.
        # We drain it here with exchange access for the full atomic workflow.
        # ================================================================
        try:
            from engine.ledger import drain_tp_cascade, handle_tp_completion
            pending_tp_cascades = drain_tp_cascade()
            if pending_tp_cascades:
                logger.info(f"[TP-DRAIN] Processing {len(pending_tp_cascades)} pending TP cascades...")
                # Registry now yields (bot_id, pair, exit_price, exit_fill_ts) 4-tuples (v2.1.0)
                for cascade_entry in pending_tp_cascades:
                    tp_bot_id = cascade_entry[0]
                    tp_pair   = cascade_entry[1]
                    tp_price  = cascade_entry[2]
                    tp_fill_ts = cascade_entry[3] if len(cascade_entry) > 3 else 0
                    try:
                        # Find the exchange for this pair
                        tp_ex = list(self.exchanges.values())[0] if self.exchanges else None
                        if tp_ex:
                            success = handle_tp_completion(
                                bot_id=tp_bot_id,
                                exit_price=tp_price,
                                pair=tp_pair,
                                exchange=tp_ex,
                                exit_fill_ts=tp_fill_ts
                            )
                            if success:
                                logger.info(f"✅ [TP-DRAIN] Bot {tp_bot_id} {tp_pair} cascade complete (cst={tp_fill_ts}).")
                                # Remove from this cycle's bot list (already reset)
                                bots = [b for b in bots if b[0] != tp_bot_id]
                            else:
                                logger.error(f"❌ [TP-DRAIN] Bot {tp_bot_id} cascade FAILED — will retry next drain.")
                        else:
                            logger.warning(f"[TP-DRAIN] No exchange available for bot {tp_bot_id}. Re-queuing.")
                            from engine.ledger import register_tp_cascade
                            register_tp_cascade(tp_bot_id, tp_pair, tp_price, tp_fill_ts)  # preserve fill_ts on retry
                    except Exception as _tp_cascade_err:
                        logger.error(f"[TP-DRAIN] Exception for bot {tp_bot_id}: {_tp_cascade_err}")
        except Exception as _drain_err:
            logger.warning(f"[TP-DRAIN] Drain loop failed (non-fatal): {_drain_err}")



        # Update workers size
        max_workers = min(len(bots) + 2, 20)

        if not hasattr(self, '_bot_executor') or self._bot_executor is None:
            self._bot_executor = BotExecutor(self)
        bot_executor = self._bot_executor

        logger.debug(f"DEBUG: Starting cycle with {len(bots)} bots")
        if not bots:
            logger.warning("No active bots found to process in this cycle.")

        if getattr(self, 'bot_pool', None) is None:
            self.bot_pool = ThreadPoolExecutor(max_workers=20)

        # Process bots using the primed cache
        raw_results = list(self.bot_pool.map(lambda b: bot_executor.process_bot(b, exchange_snapshot=exchange_snapshot), bots))

        # Filter out None results (bots skipped or errored)
        processed_bot_results = [r for r in raw_results if r is not None and r[0] is not None]

        # 🚀 FUNDAMENTAL FIX: Aggregate all trade updates for atomic DB write
        trade_updates = [res[1] for res in processed_bot_results if res[1] is not None]

        # Collect physical positions
        physical_positions = []
        for mt, snap in exchange_snapshot.items():
            physical_positions.extend(snap.get('positions', []))

        # Always update snapshot if we have data OR if we need to clear table (handled by empty list)
        # But we want to avoid spamming empty updates if nothing changed?
        # For UI sync, we MUST update.
        if True: # Always attempt sync to keep UI fresh
            try:
                update_full_snapshot(trade_updates, physical_positions)
                if len(physical_positions) > 0:
                     logger.info(f"✅ Active Positions Synced: {len(physical_positions)}")
            except Exception as e:
                logger.error(f"❌ Failed to perform atomic snapshot update: {e}")

        # 🚀 FUNDAMENTAL FIX: Active Positions are now updated atomically in 'update_full_snapshot' above.
        # We removed the redundant call here to prevent transaction races.
        # Ensure 'update_full_snapshot' is ALWAYS called even if no trade updates, if we have positions.

        # Fallback: If update_full_snapshot wasn't called (no trades, no pos?), force one?
        # Actually, if we have positions, the block above (if trade_updates or snap_pos) RUNS.
        # If snap_pos is empty, we WANT the table cleared (but cautiously, see safety checks).
        # verified: 'snap_pos' logic handles it.

        # Extract sleep intervals from results
        results = [res[0] for res in processed_bot_results]

        # ============================================================
        # LAYER 3 FIX: Periodic Position Reconciliation
        # ============================================================
        # Runs every ~60 cycles (~5 minutes at 5s intervals)
        # Catches any state desyncs that slipped through Layers 1 and 2
        # ============================================================
        if self.cycle_count % 60 == 0 and self._reconciler:
            try:
                # 🏗️ PHASE 4: Use persistent self._reconciler — no new instantiation.
                # The persistent instance carries CARRY_PENDING state awareness.
                logger.info("🔄 Running periodic position reconciliation (persistent reconciler)...")
                self._reconciler.reconcile_all()
                logger.info("🔄 Periodic position reconciliation complete")
            except Exception as e:
                logger.warning(f"Periodic reconciliation failed: {e}")

        # ============================================================
        # LAYER 4 FIX: Active Integrity Enforcement (Zombies & Orphans)
        # ============================================================
        # Runs EVERY cycle to aggressively fix state corruption.
        # 1. Adopts unclaimed physical positions (Zombies)
        # 2. Cancels stuck/orphan orders
        # 3. Fixes internal DB inconsistencies
        # ============================================================
        try:
            enforce_integrity(self, exchange_snapshot)
        except Exception as e:
            logger.error(f"Integrity check failed: {e}")

        # INV-31: Run Ground Truth Reconciler periodically
        self._gtr_cycle_counter += 1
        if self._gtr_cycle_counter >= self._gtr.CYCLE_INTERVAL:
            self._gtr_cycle_counter = 0
            try:
                with get_connection() as conn:
                    gtr_results = self._gtr.run(self.exchange, conn)

                log_msg = (
                    f"[GTR-INV31] Reconciliation pass: "
                    f"ghost={gtr_results['ghost_virtual']} "
                    f"stuck={gtr_results['stuck_cascade']} "
                    f"stuck_pending_cleared={gtr_results.get('stuck_pending_cleared', [])} "
                    f"orphan={gtr_results['orphan_physical']} "
                    f"manual_proof={gtr_results.get('manual_proof', [])} "
                    f"in_sync={gtr_results['in_sync_count']}"
                )
                if any([gtr_results['ghost_virtual'],
                        gtr_results['stuck_cascade'],
                        gtr_results.get('stuck_pending_cleared', []),
                        gtr_results['orphan_physical'],
                        gtr_results.get('manual_proof', [])]):
                    logger.warning(log_msg)
                else:
                    logger.info(log_msg)
            except Exception as e:
                logger.error(f"[GTR-INV31] Reconciliation pass failed: {e}")

        # Publish cycle time
        end_time = time.time()
        BOT_CYCLE_TIME.set(end_time - start_time)

        # AGGREGATE SMART POLLING
        # Find minimum requested sleep time. Default to 10s if no requests.
        valid_intervals = [r for r in results if isinstance(r, (int, float)) and r > 0]
        recommended_sleep = min(valid_intervals) if valid_intervals else 10.0

        return recommended_sleep