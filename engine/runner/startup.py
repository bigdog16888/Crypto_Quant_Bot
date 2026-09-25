"""
Startup orchestration mixin for BotRunner.

Contains the engine startup sequence that was previously in BotRunner's
class body in engine/runner/__init__.py. Extracted byte-for-byte
(no logic changes) as part of the engine/runner/ package split
(Module 3 of 4).

Methods moved here:
    - __init__          (constructor / instance setup)
    - _post_init        (safety baseline, startup sync, trading mode)
    - startup_sync      (blocking startup parity barrier)
    - _initialize_exchanges  (smart exchange init)
    - _initialize_safety_baseline  (drawdown baseline capture)

NOTE: These methods call other BotRunner methods (get_active_bots,
_abort_if_stop_requested) and ShutdownMixin methods (_write_pid_file)
via `self`. Those remain defined in their respective modules and resolve
through the shared MRO — no duplication.
"""

import time
import json
import sys
import os
import logging

from config.settings import config
from engine.database import (
    get_connection,
    get_starting_equity,
    check_and_fix_integrity,
    get_bot_status,
)
from engine.exchange_interface import ExchangeInterface, normalize_market_type, normalize_symbol
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.bot_executor import BotExecutor
from engine.ground_truth_reconciler import GroundTruthReconciler
from engine.reconciler import StateReconciler
from engine.shutdown_control import is_stop_requested

logger = logging.getLogger("BotRunner")


class StartupMixin:
    """Engine startup sequence: constructor, sync, exchange + safety init."""

    def _initialize_exchanges(self):
        """
        Smart initialization of exchanges based on active bots.
        Robustly handles failures (e.g. Spot API down) without crashing the engine.
        """
        try:
            active_bots = self.get_active_bots()
            required_markets = set()

            # 1. Determine required markets
            for bot in active_bots:
                # bot[5] is config_json
                if bot[5]:
                    try:
                        cfg = json.loads(bot[5])
                        m_type = normalize_market_type(cfg.get('market_type', config.MARKET_TYPE))
                        required_markets.add(m_type)
                    except: pass

            # Always add default market type (for safety/fallback)
            required_markets.add(config.MARKET_TYPE)

            # If specified global overrides
            if getattr(config, 'FUTURES_ONLY_MODE', False):
                required_markets.discard('spot')
                required_markets.add('future')

            logger.info(f"DEBUG: Required Markets: {required_markets}")

            # 2. Initialize each required market
            for m_type in required_markets:
                try:
                    logger.info(f"Initializing Exchange: {m_type}...")
                    self.exchanges[m_type] = ExchangeInterface(market_type=m_type)
                    logger.info(f"✅ Exchange {m_type} initialized.")
                except Exception as e:
                    logger.error(f"❌ Failed to initialize {m_type} exchange: {e}")
                    # Continue - don't crash engine just because one market failed

            # 3. Deep Reconciliation is handled by prime_startup_snapshot below.
            # DeepReconciler.run() was removed (2026-04): it made a redundant fetch_positions
            # call before prime_startup_snapshot(), causing a sequential startup delay and
            # double-reconciliation. prime_startup_snapshot() + reconstruct_offline_fills()
            # are the authoritative startup reconciliation path.

            # Use a single reconciler instance for subsequent checks
            # 📸 PHASE 2: prime_startup_snapshot() fetches positions ONCE atomically.
            # This replaces the separate fetch_positions loop that used to fire here
            # AND the duplicate fetch inside reconstruct_offline_fills below.
            from engine.reconciler import StateReconciler
            reconciler = StateReconciler(self.exchanges)
            try:
                logger.info("📸 [STARTUP] Priming single exchange snapshot (Phase 2 architecture)...")
                reconciler.prime_startup_snapshot()
            except Exception as e:
                logger.error(f"❌ [STARTUP] Failed to prime startup snapshot: {e}")

            # 4. Write-Ahead Logging Cleanup (Phase 3)
            # Recover any pending orders from previous crashed session.
            try:
                from engine.database import cleanup_pending_orders
                # Use the primary exchange (default market type)
                primary_ex = self.exchanges.get(config.MARKET_TYPE)
                if primary_ex:
                    wal_stats = cleanup_pending_orders(primary_ex)
                    if wal_stats['total'] > 0:
                        logger.info(f"📋 WAL Cleanup: {wal_stats['confirmed']} recovered, {wal_stats['failed']} failed out of {wal_stats['total']} pending")
            except Exception as e:
                logger.error(f"Failed to run WAL cleanup: {e}")

            # 5. Offline Fill Detection (Phase 4)
            # NOTE: reconstruct_offline_fills is called ONCE in startup_sync().
            # Removed duplicate call here (Phase 2 architecture — single offline-fill pass).

            # 6. WebSocket Stream Initialization moved to explicit startup or run_cycle
            # Logic moved to ensure reliable background thread management

        except Exception as e:
             logger.error(f"Error during smart exchange init: {e}")

    def _initialize_safety_baseline(self):
        """Captures initial account state for Drawdown monitoring."""
        # Skip baseline initialization if NO_API_MODE (can't fetch balance)
        if getattr(config, 'NO_API_MODE', False):
            logger.info("NO_API_MODE: Skipping safety baseline initialization (no API key configured)")
            self.initial_equity = 0.0
            return

        try:
            # === CRITICAL FIX: Use DB STARTING_EQUITY as true baseline ===
            total_stablecoin = get_starting_equity()
            active_bots = [b for b in self.get_active_bots() if b[9] == 1]

            # 1. Invested Cost
            invested_sum = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and t_data.get('total_invested'):
                    invested_sum += float(t_data['total_invested'])

            # Initial equity is the fixed baseline + invested cost
            self.initial_equity = total_stablecoin + invested_sum
            logger.info(f"Safety Baseline Initialized. Equity: ${self.initial_equity:.2f} (Base: {total_stablecoin:.2f} + Pos: {invested_sum:.2f})")

        except Exception as e:
            logger.error(f"Failed to initialize safety baseline: {e}")
            self.initial_equity = 0.0

    def _post_init(self):
        """Post-initialization: safety baseline, startup sync, trading mode."""
        self._initialize_safety_baseline()

        # State Synchronization
        try:
            logger.info("Starting Startup Sync...")
            self.startup_sync()
            if self._abort_if_stop_requested("startup-sync"):
                logger.info("Startup sync aborted by stop signal.")
            else:
                logger.info("Startup Sync Complete")
        except Exception as e:
            logger.critical(f"🛑 FATAL: Startup Sync Failed: {e}", exc_info=True)
            if config.TESTING_MODE:
                logger.warning("⚠️ [STARTUP-BARRIER-FAIL] Bypassing fatal abort in TESTING_MODE.")
            else:
                self.running = False
                import sys
                sys.exit(1)

        # Safe Monitor Mode: Disable execution if flag is False
        self.trading_enabled = getattr(config, 'TRADING_ENABLED', False)
        if not self.trading_enabled:
            logger.warning("🛡️ SAFE MONITOR MODE ACTIVE: Trading logic will run but orders are BLOCKED.")
        else:
            logger.info("🚀 TRADING MODE ACTIVE: Full order execution enabled.")

    def _classify_foreign_positions(self, parity_ex, pairs):
        """
        Read-only diagnostic: split live exchange positions into symbols that
        match an active bot pair vs foreign symbols (e.g. USDS-M synthetic
        seed rows like BNBUSD after a testnet/demo reset, or manual positions).

        Returns (foreign, matched) where each entry is
        (raw_symbol, normalized_symbol, signed_net_qty).

        Foreign positions are NEVER touched here — they are only surfaced so
        the operator can see why a parity gate fired. The audit
        (audit_pair_ledger_vs_exchange) only iterates bot pairs, so foreign
        symbols are otherwise invisible in the barrier log.
        """
        foreign, matched = [], []
        try:
            positions = parity_ex.fetch_positions()
        except Exception as e:
            logger.warning(f"⚠️ [STARTUP-BARRIER] Foreign-symbol scan skipped (fetch_positions failed): {e}")
            return foreign, matched
        if not positions:
            return foreign, matched

        bot_norms = {normalize_symbol(p).upper() for p in pairs}
        by_norm = {}
        for pos in positions:
            raw = pos.get('symbol', '')
            net = pos.get('net_qty')
            if net is None or net == 0:
                net = pos.get('contracts')
            if net is None or net == 0:
                qty = float(pos.get('qty', pos.get('size', 0)) or 0)
                side = str(pos.get('side', '')).lower()
                net = -qty if side in ('short', 'sell') else qty
            net = float(net or 0)
            if abs(net) < 1e-12:
                continue
            norm = normalize_symbol(raw).upper()
            by_norm.setdefault(norm, []).append((raw, net))

        for norm, plist in sorted(by_norm.items()):
            total = sum(x[1] for x in plist)
            raws = sorted({x[0] for x in plist})
            entry = (raws[0] if len(raws) == 1 else '/'.join(raws), norm, total)
            if norm in bot_norms:
                matched.append(entry)
            else:
                foreign.append(entry)
        return foreign, matched

    def _startup_pair_plausibility_gate(self, exchange, conn, active_bot_ids):
            """
            O-9 (Step 2.5): Matched-pair plausibility gate.

            Before seal_all_active_bots() and wipe_bot_ghost() run, check each active
            bot's recorded position (trades.open_qty) against the exchange's signed
            physical net for that bot's PAIR. The 2026-08-19 incident hit a MATCHED
            pair (10007/BNBUSDC): the exchange showed an implausible size relative to
            the DB claim, and seal/wipe ran destructively. 9132dc8 only handled the
            FOREIGN-symbol case; this gate closes the matched-pair case.

            Returns a set of bot_ids for which seal/wipe MUST be blocked this startup
            (pair-level only — the rest of the engine is untouched). Read-only:
            never mutates the DB or exchange.
            """
            if not getattr(config, 'STARTUP_PLAUSIBILITY_GATE', True):
                logger.info("⏭️ [STARTUP-BARRIER] STARTUP_PLAUSIBILITY_GATE=False — plausibility gate disabled.")
                return set()

            from engine.database import get_bot_status
            from engine.parity_gates import get_exchange_signed_net, qty_tolerance

            tol = qty_tolerance()
            blocked = set()

            # 🛡️ PAIR-LEVEL PLAUSIBILITY (2026-09-25): In hedge setups, parent bots
            # hold the position while hedge children have open_qty=0. Evaluating per-bot
            # falsely blocks children. Fix: aggregate all ACTIVE bots on the same pair
            # and compare the NET signed sum to exchange physical.
            pair_to_bot_ids = {}
            bot_meta = {}
            for bot_id in active_bot_ids:
                try:
                    t = get_bot_status(bot_id)
                    if not t or not t.get('pair') or not t.get('is_active'):
                        continue
                    pair = t['pair']
                    direction = str(t.get('direction') or 'LONG').upper()
                    open_qty = float(t.get('open_qty') or 0)
                    pair_to_bot_ids.setdefault(pair, []).append(bot_id)
                    bot_meta[bot_id] = (pair, direction, open_qty)
                except Exception as _stat_err:
                    logger.warning(f"⚠️ [PLAUSIBILITY] Bot {bot_id}: status read failed ({_stat_err}) — not gated.")
                    continue

            for pair, bot_ids in pair_to_bot_ids.items():
                # Sum signed open_qty across ALL active bots on this pair
                db_signed_sum = 0.0
                for bid in bot_ids:
                    p, direction, open_qty = bot_meta[bid]
                    signed = open_qty if direction == 'LONG' else -open_qty
                    db_signed_sum += signed

                physical = get_exchange_signed_net(exchange, pair)
                if physical is None or physical == 'mock_unconfigured':
                    logger.info(f"⏭️ [PLAUSIBILITY] Pair {pair}: no exchange data — defer to Step 8 strict audit")
                    continue

                abs_db, abs_phys = abs(db_signed_sum), abs(physical)
                real = lambda x: x > tol
                reason = None
                if real(abs_db) and not real(abs_phys):
                    reason = f"DB pair net {db_signed_sum:+.6f} but exchange is flat ({physical:+.6f})"
                elif real(abs_phys) and not real(abs_db):
                    reason = f"exchange holds {physical:+.6f} but DB pair net is flat ({db_signed_sum:+.6f})"
                elif real(abs_db) and real(abs_phys) and (db_signed_sum > tol) != (physical > tol):
                    reason = (f"sign mismatch: DB pair net={db_signed_sum:+.6f} exchange={physical:+.6f} "
                              f"(one-way mode cannot hold both sides)")

                if reason:
                    # Block ALL bots on this pair
                    for bid in bot_ids:
                        logger.error(
                            f"⛔ [STARTUP-BARRIER] [PLAUSIBILITY-BLOCK] Bot {bid} on {pair}: {reason}. "
                            f"Blocking seal/wipe for THIS pair only. See docs/OPERATOR_MISMATCH_RUNBOOK.md Pattern E. "
                            f"Not touched by the engine."
                        )
                        blocked.add(bid)
                else:
                    for bid in bot_ids:
                        _, direction, open_qty = bot_meta[bid]
                        signed = open_qty if direction == 'LONG' else -open_qty
                        logger.info(
                            f"✅ [PLAUSIBILITY-OK] Bot {bid} {pair}: bot_signed={signed:+.6f} "
                            f"pair_net={db_signed_sum:+.6f} exchange={physical:+.6f} gap={physical-db_signed_sum:+.6f}"
                        )

            if blocked:
                logger.warning(
                    f"🌐 [STARTUP-BARRIER] {len(blocked)} matched-pair bot(s) blocked from seal/wipe by plausibility gate: {sorted(blocked)}"
                )
            return blocked


    def startup_sync(self):
        """
        Strict, blocking startup synchronization barrier.
        Forces the local database ledger and cache to match the exchange truth,
        heals any mismatches, purges phantom residues, and guarantees that 100%
        of active pairs are in perfect parity before lifting the barrier.
        """
        logger.info("🔄 [STARTUP-SYNC] Entering strict, blocking startup barrier...")
        if self._abort_if_stop_requested("startup-sync"):
            return

        try:
            # Get exchange instance (usually 'future')
            parity_ex = None
            for _mt, _ex in self.exchanges.items():
                if _ex:
                    parity_ex = _ex
                    break

            if not parity_ex:
                raise RuntimeError("No active exchange interface configured for startup barrier.")

            from engine.database import (
                heal_inflated_filled_amounts,
                consolidate_duplicate_bot_orders,
                verify_filled_orders_against_exchange,
                sync_trades_from_orders,
                update_active_positions_snapshot,
                audit_pair_ledger_vs_exchange,
                flag_pair_ledger_mismatch
            )
            from engine.ledger import seal_all_active_bots
            from engine.oneway_netting import reconcile_oneway_pair_open_qty, sync_pair_to_exchange, detect_bot_ghost, wipe_bot_ghost
            from engine.parity_gates import detect_and_repair_global_wipe, startup_repair_mismatched_pairs
            from engine.startup_repair_verification import (
                _pair_has_unexplained_orphan,
                _mismatch_explainable_by_cid,
            )

            conn = get_connection()
            active_ids = [r[0] for r in conn.execute("SELECT id FROM bots WHERE is_active=1").fetchall()]
            pairs = [r[0] for r in conn.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1").fetchall()]

            # -------------------------------------------------------------
            # STEP 1: Core Database Cleaning & Pre-Flight Order Checks
            # -------------------------------------------------------------
            logger.info("🧹 [STARTUP-BARRIER] [1/8] Cleaning database fills and duplicates...")
            heal_inflated_filled_amounts()
            consolidate_duplicate_bot_orders()

            # -------------------------------------------------------------
            # STEP 2: Exchange Fill Verification & Offline Fill Reconstruction
            # -------------------------------------------------------------
            logger.info("📡 [STARTUP-BARRIER] [2/8] Syncing recent/historical fills from exchange...")
            verify_filled_orders_against_exchange(parity_ex)

            # Compute offline duration from last clean-shutdown timestamp
            _shutdown_ts_file = 'last_shutdown.ts'
            _offline_hours = 168.0  # safe default (7 days)
            if os.path.exists(_shutdown_ts_file):
                try:
                    with open(_shutdown_ts_file) as _sf:
                        _last_shutdown = int(_sf.read().strip())
                    _offline_hours = max(2.0, (time.time() - _last_shutdown) / 3600.0 + 1.0)
                    logger.info(f"[STARTUP-BARRIER] Outage window: ~{_offline_hours:.1f}h. Scanning offline fills...")
                except Exception as _ts_err:
                    logger.warning(f"[STARTUP-BARRIER] Could not read last_shutdown.ts: {_ts_err}. Defaulting to 168h.")
            _scan_hours = min(_offline_hours, 168.0)

            if self._reconciler:
                stats = self._reconciler.reconstruct_offline_fills(since_hours=int(_scan_hours))
                logger.info(f"✅ [STARTUP-BARRIER] Offline fills reconstruction complete: {stats}")

            # -------------------------------------------------------------
            # STEP 2.5 (O-9): Matched-pair plausibility gate (before destructive seal/wipe)
            # -------------------------------------------------------------
            _plaus_blocked = self._startup_pair_plausibility_gate(parity_ex, conn, active_ids)

            # -------------------------------------------------------------
            # STEP 3: Ledger Sealing & Cache Propagation
            # -------------------------------------------------------------
            logger.info("🔒 [STARTUP-BARRIER] [3/8] Sealing all active bots from ledger fills...")
            seal_all_active_bots(skip_bot_ids=_plaus_blocked)
            for bid in active_ids:
                sync_trades_from_orders(bid)
            logger.info("✅ [STARTUP-BARRIER] Sealing and trades cache propagation complete.")

            # -------------------------------------------------------------
            # STEP 4: Global Wipe Checks & Ghost/Netting Alignment
            # -------------------------------------------------------------
            logger.info("⚖️ [STARTUP-BARRIER] [4/8] Running global wipe and ghost repairs...")
            detect_and_repair_global_wipe(parity_ex)
            for _pair in pairs:
                sync_pair_to_exchange(_pair, parity_ex, conn)
            for bid in active_ids:
                if bid in _plaus_blocked:
                    logger.warning(f"⛔ [STARTUP-BARRIER] Skipping ghost wipe for bot {bid} (plausibility-gated).")
                    continue
                if detect_bot_ghost(parity_ex, bid, conn):
                    wipe_bot_ghost(parity_ex, bid, conn)

            # -------------------------------------------------------------
            # STEP 5: One-way open_qty Netting Reconciliation
            # -------------------------------------------------------------
            logger.info("⚖️ [STARTUP-BARRIER] [5/8] Aligning cross-bot netting open quantities...")
            for _pair in pairs:
                _msg = reconcile_oneway_pair_open_qty(parity_ex, _pair)
                if _msg:
                    logger.warning(f"  [{_pair}]: {_msg}")

            # -------------------------------------------------------------
            # STEP 6: Prime Physical Position Snapshot (SNAP-ALLOCATE)
            # -------------------------------------------------------------
            logger.info("📡 [STARTUP-BARRIER] [6/8] Priming active_positions snapshot (SNAP-ALLOCATE)...")
            _snap = parity_ex.fetch_positions()
            if _snap is not None:
                update_active_positions_snapshot(_snap, force_write=True)
                logger.info(f"✅ [STARTUP-BARRIER] SNAP-ALLOCATE primed successfully ({len(_snap)} positions).")
            else:
                logger.warning("⚠️ [STARTUP-BARRIER] Could not retrieve position snapshot; using database cached snapshot.")

            # -------------------------------------------------------------
            # STEP 7: Pair Parity Repair (Deflate, Orphan Flatten, Phantom Purges)
            # -------------------------------------------------------------
            logger.info("🔧 [STARTUP-BARRIER] [7/8] Running pair parity repairs...")
            _repair_summary = startup_repair_mismatched_pairs(parity_ex)
            logger.info(f"✅ [STARTUP-BARRIER] Repair sequence finished: {_repair_summary}")

            # -------------------------------------------------------------
            # STEP 8: Final Parity Verification & Strict Block
            # -------------------------------------------------------------
            logger.info("🔍 [STARTUP-BARRIER] [8/8] Verifying final pair parity...")
            _mismatches = audit_pair_ledger_vs_exchange(parity_ex)

            # Symbol-mismatch visibility: surface exchange positions on symbols no
            # active bot trades (e.g. USDS-M synthetic seed rows like BNBUSD after
            # a testnet/demo reset). The pair audit only iterates bot pairs, so
            # these are otherwise invisible. Read-only — never modifies anything.
            _foreign, _foreign_matched = self._classify_foreign_positions(parity_ex, pairs)
            if _foreign:
                logger.warning(
                    f"🌐 [STARTUP-BARRIER] {len(_foreign)} exchange position(s) on symbols with NO active bot "
                    f"(foreign/synthetic — e.g. testnet reset seed rows). Ignored by the pair audit; "
                    f"NOT touched by the engine:"
                )
                for _raw, _norm, _net in _foreign:
                    logger.warning(f"   FOREIGN  {_raw} (norm={_norm}) net_qty={_net:+.6f}")

            if _mismatches:
                _critical = flag_pair_ledger_mismatch(_mismatches, exchange=parity_ex)
                for _p, _v, _ph, _d in _mismatches:
                    logger.error(f"❌ [STARTUP-BARRIER-FAIL] {_p}: ledger={_v:.6f} exchange={_ph:.6f} delta={_d:.6f}")

                # ---------------------------------------------------------
                # STEP 8.5 (O-9 CID self-heal): classify critical mismatches.
                # Daily-shutdown rule — routine overnight drift from CID-traceable
                # fills must self-heal automatically; only genuinely unexplainable
                # divergence (orphaned position / no CID accounting) blocks startup.
                # See docs/DAILY_SHUTDOWN_SELF_HEALING.md.
                # ---------------------------------------------------------
                _genuine_anomalies = []
                if _critical:
                    # Narrow exclusion list: pairs whose ENTIRE active bot roster is
                    # explicitly named in config.STARTUP_EXCLUDED_BOT_IDS are allowed
                    # through with a loud warning (frozen bots awaiting manual review).
                    # The barrier stays fully strict for every other pair and for any
                    # future anomaly. See config/settings.py STARTUP_EXCLUDED_BOT_IDS.
                    _excluded_ids = getattr(config, 'STARTUP_EXCLUDED_BOT_IDS', set())

                    def _pair_fully_excluded(_pair):
                        """Return (True, [bot_ids]) iff every active bot on _pair is in
                        the exclusion list; (False, []) otherwise."""
                        if not _excluded_ids:
                            return False, []
                        _norm = normalize_symbol(_pair).upper()
                        _pair_bots = [r[0] for r in conn.execute(
                            "SELECT id FROM bots WHERE is_active=1 AND (pair=? OR normalized_pair=?)",
                            (_pair, _norm)).fetchall()]
                        if not _pair_bots:
                            return False, []
                        return all(b in _excluded_ids for b in _pair_bots), _pair_bots

                    for _p, _v, _ph, _d in _critical:
                        try:
                            _has_orphan = _pair_has_unexplained_orphan(_p, _ph, parity_ex)
                            _explainable = _mismatch_explainable_by_cid(_p, _v, _ph, parity_ex)
                        except Exception as _cid_err:
                            logger.error(f"[STARTUP-BARRIER] {_p}: CID verdict errored ({_cid_err}) — treating as genuine anomaly.")
                            _genuine_anomalies.append((_p, _v, _ph, _d))
                            continue

                        if _has_orphan or not _explainable:
                            # Check the narrow exclusion list before blocking
                            _is_excluded, _excl_bots = _pair_fully_excluded(_p)
                            if _is_excluded:
                                logger.critical(
                                    f"⚠️⚠️ [STARTUP-BARRIER-EXCLUSION] {_p}: GENUINE ANOMALY "
                                    f"(unexplained_orphan={_has_orphan}, cid_explainable={_explainable}, "
                                    f"delta={_d:+.6f}) BUT all {len(_excl_bots)} active bots {_excl_bots} are on the "
                                    f"explicit STARTUP_EXCLUDED_BOT_IDS list. Skipping block — these bots stay "
                                    f"frozen at REQUIRE_MANUAL_PROOF and will NOT cycle. MANUAL REVIEW STILL REQUIRED."
                                )
                                continue
                            _genuine_anomalies.append((_p, _v, _ph, _d))
                            logger.error(
                                f"🚨 [STARTUP-BARRIER] {_p}: GENUINE ANOMALY — "
                                f"unexplained_orphan={_has_orphan}, cid_explainable={_explainable}. "
                                f"Blocking startup; manual review required."
                            )
                        else:
                            logger.warning(
                                f"🩹 [STARTUP-BARRIER] {_p}: routine CID-traceable drift "
                                f"(delta={_d:+.6f}). Attempting targeted offline-fill self-heal..."
                            )
                            # Credit the CID-matched fills for this pair, then re-audit.
                            if self._reconciler:
                                try:
                                    _heal_stats = self._reconciler.reconstruct_offline_fills(
                                        since_hours=int(_scan_hours),
                                        pair_filter=normalize_symbol(_p),
                                    )
                                    logger.info(f"🩹 [STARTUP-BARRIER] {_p}: self-heal reconstruction: {_heal_stats}")
                                except Exception as _heal_err:
                                    logger.error(f"🩹 [STARTUP-BARRIER] {_p}: self-heal reconstruction failed ({_heal_err}) — treating as genuine anomaly.")
                                    _genuine_anomalies.append((_p, _v, _ph, _d))
                                    continue
                            # Re-audit this pair after crediting. If parity now holds, it's cleared.
                            _recheck = audit_pair_ledger_vs_exchange(parity_ex)
                            _still_bad = [m for m in _recheck if m[0] == _p]
                            if _still_bad:
                                logger.error(
                                    f"🚨 [STARTUP-BARRIER] {_p}: still mismatched after self-heal "
                                    f"({_still_bad[0][1]:.6f} vs {_still_bad[0][2]:.6f}) — escalating to genuine anomaly."
                                )
                                _genuine_anomalies.append((_p, _v, _ph, _d))
                            else:
                                logger.info(f"✅ [STARTUP-BARRIER] {_p}: self-healed to parity. Cleared.")

                if _genuine_anomalies:
                                    # TRUE PAIR-LEVEL QUARANTINE: Instead of global crash, flag only
                                    # the anomalous pairs and allow clean pairs to proceed.
                                    _quarantined_pairs = []
                                    _clean_pairs = []

                                    # Separate critical mismatches into quarantined vs clean
                                    _all_critical_pairs = set(_p for _p, _v, _ph, _d in _critical)

                                    for _p, _v, _ph, _d in _genuine_anomalies:
                                        _quarantined_pairs.append((_p, _v, _ph, _d))
                                        # Flag all active bots on this pair as REQUIRE_MANUAL_PROOF
                                        _norm = normalize_symbol(_p).upper()
                                        _pair_bots = [r[0] for r in conn.execute(
                                            "SELECT id FROM bots WHERE is_active=1 AND (pair=? OR normalized_pair=?)",
                                            (_p, _norm)).fetchall()]
                                        for _bid in _pair_bots:
                                            conn.execute(
                                                "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=? AND status IN ('Scanning','ACTIVE','IN TRADE')",
                                                (_bid,)
                                            )
                                            logger.critical(f"🔒 [QUARANTINE] Pair {_p}: Bot {_bid} set to REQUIRE_MANUAL_PROOF (delta={_d:+.6f})")
                                        conn.commit()

                                    # Determine clean pairs (those in _critical but not in _genuine_anomalies)
                                    for _p, _v, _ph, _d in _critical:
                                        if _p not in [_gp[0] for _gp in _genuine_anomalies]:
                                            _clean_pairs.append((_p, _v, _ph, _d))

                                    # Also count fleet-wide pairs NOT in _critical at all as clean
                                    # (the _clean_pairs list above only covers pairs that made it into
                                    # the mismatch list — pairs in perfect parity are invisible to it)
                                    _critical_pair_norms = set(
                                        normalize_symbol(_p).upper() for _p, _, _, _ in _critical
                                    )
                                    for _fp in conn.execute(
                                        "SELECT DISTINCT pair FROM bots WHERE is_active=1"
                                    ).fetchall():
                                        _fp_norm = normalize_symbol(_fp[0]).upper()
                                        if _fp_norm not in _critical_pair_norms:
                                            _clean_pairs.append((_fp[0], 0.0, 0.0, 0.0))

                                    if config.TESTING_MODE:
                                        logger.warning("⚠️ [STARTUP-BARRIER-FAIL] Genuine anomaly detected on startup, but TESTING_MODE is active. Bypassing strict exit.")
                                    elif _clean_pairs:
                                        # At least one clean pair exists — proceed with quarantined pairs isolated
                                        logger.critical(
                                            f"⚠️ [STARTUP-QUARANTINE] {len(_quarantined_pairs)} pair(s) quarantined: "
                                            f"{', '.join(f'{p}(delta={d:+.2f})' for p, v, ph, d in _quarantined_pairs)}. "
                                            f"{len(_clean_pairs)} clean pair(s) proceeding to TRADING MODE ACTIVE."
                                        )
                                        logger.warning(
                                            f"🔒 Quarantined pairs: {', '.join(_qp[0] for _qp in _quarantined_pairs)}. "
                                            f"Clean pairs: {', '.join(_cp[0] for _cp in _clean_pairs)}."
                                        )
                                        # Continue past barrier — clean pairs will trade, quarantined stay frozen
                                    else:
                                        # ZERO clean pairs — full block as before
                                        _foreign_hint = ""
                                        if _foreign:
                                            _foreign_hint = (
                                                " Foreign (non-bot) symbols present on exchange: "
                                                + ", ".join(f"{_r}({_n:+.4f})" for _r, _nm, _n in _foreign)
                                                + ". If these are testnet/demo reset seed rows, clear or ignore them per "
                                                "docs/OPERATOR_MISMATCH_RUNBOOK.md before restarting."
                                            )
                                        raise RuntimeError(
                                            f"Startup parity verification FAILED for {len(_genuine_anomalies)} genuine-anomaly pair(s). "
                                            f"{_foreign_hint}"
                                            "Engine cannot start in a mismatched state. Run scripts/run_startup_heal.py or resolve manually."
                                        )
                elif _critical:
                    logger.info("✅ [STARTUP-BARRIER] All critical mismatches were routine CID-traceable drift and self-healed. Startup barrier cleared.")
                else:
                    logger.info("✅ [STARTUP-BARRIER] All mismatches successfully isolated. Startup barrier cleared.")
            else:
                logger.info("✅ [STARTUP-BARRIER] All pairs verified in perfect parity. Startup barrier cleared.")

            # Cleanup stray/manual orders
            logger.info("🧹 [STARTUP-CLEANUP] Scanning for ghost orders to cancel...")
            total_cancelled = 0
            allowed_bot_ids = {str(bid) for bid in active_ids}
            try:
                orders = parity_ex.fetch_open_orders()
                if orders:
                    for o in orders:
                        cid = o.get('clientOrderId', '')
                        bot_id = None
                        if cid.startswith('CQB_'):
                            parts = cid.split('_')
                            if len(parts) > 1:
                                bot_id = parts[1]
                        should_cancel = False
                        reason = ""
                        if bot_id:
                            if bot_id not in allowed_bot_ids:
                                should_cancel = False
                                logger.info(f"Preserving stray CQB order {o['id']} (bot {bot_id}) for recovery.")
                        else:
                            if getattr(config, 'STRICT_CLEANUP', True):
                                should_cancel = True
                                reason = "Manual/Unknown Order (Strict Mode)"
                        if should_cancel:
                            logger.warning(f"🚫 Cancelling Ghost Order {o['id']} ({o['symbol']}): {reason}")
                            parity_ex.cancel_order(o['id'], o['symbol'])
                            total_cancelled += 1
            except Exception as _ord_err:
                logger.warning(f"⚠️ Ghost order scan failed: {_ord_err}")

            if total_cancelled > 0:
                logger.info(f"✅ Ghost order cleanup completed: {total_cancelled} orders cancelled.")

            # -------------------------------------------------------------
            # WS Cache Warmup & Reconciler Align
            # -------------------------------------------------------------
            if self._reconciler:
                WS_WARMUP_SECONDS = 20
                logger.info(f"⏳ [STARTUP-RECON] Waiting {WS_WARMUP_SECONDS}s for WS cache to warm up before final reconcile...")
                time.sleep(WS_WARMUP_SECONDS)

                # Final position refresh post-warmup
                try:
                    _fresh_snap = parity_ex.fetch_positions()
                    if _fresh_snap is not None:
                        update_active_positions_snapshot(_fresh_snap, force_write=True)
                except Exception as _snap_w_err:
                    logger.warning(f"⚠️ Post-warmup snapshot refresh failed: {_snap_w_err}")

                logger.info("🛡️ [STARTUP-RECON] Executing final reconciliation check...")
                self._reconciler.reconcile_all()
                self._reconciler._align_memory_to_ledger()
                logger.info("✅ [STARTUP-RECON] Final reconciliation check complete.")

        except Exception as e:
            logger.error(f"❌ [STARTUP-SYNC] Failed: {e}")
            raise

    def __init__(self):
        # ADAPTATION: original code referenced the class by name (BotRunner._instance).
        # As a mixin method, the concrete class is not in scope, so we use
        # self.__class__ — this sets _instance on the actual BotRunner class.
        self.__class__._instance = self
        self.running = False
        import time
        self.started_at = time.time()
        # BotExecutor instance (lazy initialization for strategy caching)
        self._bot_executor: BotExecutor | None = None

        # Main thread exchanges (kept for global ops like check_circuit_breaker)
        # Smart Initialization: Only load what is needed
        self.exchanges = {}
        self._initialize_exchanges()

        # For backward compatibility and global actions
        self.exchange = self.exchanges.get(config.MARKET_TYPE, None)
        if not self.exchange:
            # Fallback to first available if default is missing
            if self.exchanges:
                self.exchange = list(self.exchanges.values())[0]
                logger.critical("NO EXCHANGES INITIALIZED! Engine cannot run.")
                sys.exit(1)

        # --- FUNDAMENTAL FIX: EARLY INTEGRITY CHECK ---
        # Ensure DB is clean before any logic runs
        try:
            from engine.database import check_and_fix_integrity
            check_and_fix_integrity()
        except Exception as e:
            logger.error(f"Startup integrity check failed: {e}")

        # --- READ-ONLY: bots.direction vs trades.position_side divergence check ---
        # Non-fatal at startup (see check_direction_side_consistency docstring in
        # engine/integrity.py for the documented surfacing decision). Divergence
        # means pair-net and bot-contribution sign conventions disagree — parity
        # math is unreliable until a human inspects. Never mutates state here.
        try:
            from engine.integrity import check_direction_side_consistency
            _divs = check_direction_side_consistency()
            if _divs:
                logger.error(
                    f"⚠️ [STARTUP] {len(_divs)} bots.direction/trades.position_side "
                    f"divergence(s) detected — see [DIR-SIDE-DIVERGENCE] errors above."
                )
        except Exception as e:
            logger.error(f"Startup direction/position_side check failed (non-fatal): {e}")

        # --- v2.0 SCHEMA MIGRATION ---
        # Idempotent: safe to run on every startup.
        # Adds cumulative_filled, position_side, cycle_id columns to bot_orders if missing.
        try:
            from engine.migrations.migration_001_v2_schema import run as _run_migration
            _m_result = _run_migration()
            if _m_result.get('applied'):
                logger.info(f"✅ [MIGRATION] Applied schema changes: {_m_result['applied']}")
        except Exception as _m_err:
            logger.warning(f"⚠️ [MIGRATION] v2.0 schema migration skipped (non-fatal): {_m_err}")

        # --- v3.9.10 UNIQUE client_order_id MIGRATION ---
        # Idempotent: safe to run on every startup.
        # Resolves any duplicate CIDs and establishes unique index on (bot_id, client_order_id).
        try:
            from engine.migrations.migration_002_unique_cid import run as _run_migration_2
            _m2_result = _run_migration_2()
            if _m2_result.get('applied'):
                logger.info(f"✅ [MIGRATION] Applied client_order_id unique index changes: {_m2_result['applied']}")
        except Exception as _m2_err:
            logger.warning(f"⚠️ [MIGRATION] Unique client_order_id migration skipped (non-fatal): {_m2_err}")

        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}

        # Safety / Circuit Breaker State
        self.initial_equity = 0.0
        self.circuit_breaker_triggered = False

        # CRITICAL: Enable trading (mission execution gate)
        self.trading_enabled = config.TRADING_ENABLED

        # 🚀 TTL Cache for multi-timeframe OHLCV data
        # Format: {(pair, timeframe): {'data': DataFrame, 'fetched_at': float}}
        self._tf_cache = {}

        # ========== RUNAWAY ORDER PROTECTION ==========
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()

        # UI Synchronization: Write PID file so Streamlit knows we are running
        self._write_pid_file()

        # INV-31: Ground Truth Reconciler initialization
        from engine.ground_truth_reconciler import GroundTruthReconciler
        self._gtr = GroundTruthReconciler()
        self._gtr_cycle_counter = 0

        # LAYER 3 FIX: Cycle counter for periodic reconciliation
        self.cycle_count = 0

        # ASYNC FLATTEN: Track how many cycles each bot has waited for its
        # pending_close market order to be confirmed filled.  Keyed by bot_id.
        # Cleared when the order fills or the bot is wiped.
        self.pending_close_cycles: dict = {}

        # Persistent reconciler instance for offline fill detection
        try:
            from engine.reconciler import StateReconciler
            self._reconciler = StateReconciler(exchanges=self.exchanges)
        except Exception as _rec_err:
            logger.warning(f"Could not initialize StateReconciler: {_rec_err}")
            self._reconciler = None

        # Complete startup: safety baseline, state sync, trading mode
        self._post_init()