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
from engine.exchange_interface import ExchangeInterface, normalize_market_type
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
            # STEP 3: Ledger Sealing & Cache Propagation
            # -------------------------------------------------------------
            logger.info("🔒 [STARTUP-BARRIER] [3/8] Sealing all active bots from ledger fills...")
            seal_all_active_bots()
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
                update_active_positions_snapshot(_snap)
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
            if _mismatches:
                _critical = flag_pair_ledger_mismatch(_mismatches, exchange=parity_ex)
                for _p, _v, _ph, _d in _mismatches:
                    logger.error(f"❌ [STARTUP-BARRIER-FAIL] {_p}: ledger={_v:.6f} exchange={_ph:.6f} delta={_d:.6f}")

                if _critical:
                    if config.TESTING_MODE:
                        logger.warning("⚠️ [STARTUP-BARRIER-FAIL] Critical mismatch detected on startup, but TESTING_MODE is active. Bypassing strict exit.")
                    else:
                        # Block start and raise error to abort startup
                        raise RuntimeError(
                            f"Startup parity verification FAILED for {len(_critical)} critical pair(s). "
                            "Engine cannot start in a mismatched state. Run scripts/run_startup_heal.py or resolve manually."
                        )
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
                        update_active_positions_snapshot(_fresh_snap)
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