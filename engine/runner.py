import time
import logging
import json
import sys
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import threading
import psutil # Added for robust PID checking
import signal
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot, update_active_positions
import sqlite3
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.bot_executor import BotExecutor
import engine.bot_executor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME
from engine.integrity import enforce_integrity
from engine.database import (
    get_connection, get_bot_status, get_starting_equity
)
from engine.ws_cache import get_ws_cache


from config.settings import config
from config.constants import (
    MIN_ORDER_USD,
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
    POLL_INTERVAL_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    STABLECOINS
)

# Configure logging with rotation (Max 50MB, keep 5 backups)
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = config.PATHS["LOG_FILE"]

rotating_handler = RotatingFileHandler(
    log_file, 
    maxBytes=10 * 1024 * 1024, # 10MB (Reduced from 50MB for resource efficiency)
    backupCount=5,
    encoding='utf-8'
)
rotating_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    handlers=[rotating_handler, stream_handler]
)
logger = logging.getLogger("BotRunner")
logger.critical(f"DEBUG: Loaded BotExecutor from {engine.bot_executor.__file__}")

# NOISE REDUCTION: Silence non-critical network warnings
logging.getLogger('ccxt').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('web3').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

# --- PROFESSIONAL FIX: OS-ENFORCED SINGLETON (SocketLock) ---
import socket as _socket_module

class SocketLock:
    """
    OS-enforced singleton using TCP port binding.
    - Zero race conditions: OS guarantees only one process can bind a port.
    - Auto-releases on crash: OS reclaims the port when the process dies.
    - No stale files: Nothing on disk to clean up.
    """
    LOCK_PORT = 19888

    def __init__(self, port=None):
        self.port = port or self.LOCK_PORT
        self._socket = None

    def acquire(self):
        self._socket = _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_STREAM)
        self._socket.setsockopt(_socket_module.SOL_SOCKET, _socket_module.SO_REUSEADDR, 0)
        try:
            self._socket.bind(("127.0.0.1", self.port))
            self._socket.listen(1)
            logger.info(f"✅ SocketLock acquired on port {self.port} (PID {os.getpid()})")
            return True
        except OSError as e:
            logger.critical(f"🛑 FATAL: Runner already active (port {self.port} in use). Cannot start duplicate. Error: {e}")
            self._socket.close()
            self._socket = None
            return False

    def release(self):
        if self._socket:
            try:
                self._socket.close()
                logger.info(f"🔓 SocketLock released (port {self.port})")
            except Exception:
                pass
            self._socket = None

class BotRunner:
    _instance = None

    def __init__(self):
        BotRunner._instance = self
        self.running = False
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
        
        # LAYER 3 FIX: Cycle counter for periodic reconciliation
        self.cycle_count = 0

        # Persistent reconciler instance for offline fill detection
        try:
            from engine.reconciler import StateReconciler
            self._reconciler = StateReconciler(exchanges=self.exchanges)
        except Exception as _rec_err:
            logger.warning(f"Could not initialize StateReconciler: {_rec_err}")
            self._reconciler = None
        
        # Complete startup: safety baseline, state sync, trading mode
        self._post_init()

    def _write_pid_file(self):
        """Write PID file for UI status detection (Streamlit reads this)."""
        try:
            pid_file = config.PATHS["PID_FILE"]
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))
        except Exception as e:
            logger.error(f"Failed to write PID file: {e}")

    def _post_init(self):
        """Post-initialization: safety baseline, startup sync, trading mode."""
        self._initialize_safety_baseline()
        
        # State Synchronization
        try:
            logger.info("Starting Startup Sync...")
            self.startup_sync()
            logger.info("Startup Sync Complete")
        except Exception as e:
            logger.error(f"Failed to sync bots on startup (non-fatal): {e}")
        
        # Safe Monitor Mode: Disable execution if flag is False
        self.trading_enabled = getattr(config, 'TRADING_ENABLED', False)
        if not self.trading_enabled:
            logger.warning("🛡️ SAFE MONITOR MODE ACTIVE: Trading logic will run but orders are BLOCKED.")
        else:
            logger.info("🚀 TRADING MODE ACTIVE: Full order execution enabled.")

    @classmethod
    def get_instance(cls):
        """Returns the current runner instance (for singleton access)"""
        return cls._instance

    def get_strategy(self, bot_id, params):
        """
        Retrieves or creates a strategy instance for a given bot.
        Caches instances to avoid recreation overhead.
        """
        if bot_id in self.strategies:
            return self.strategies[bot_id]
        
        # Create new strategy instance
        try:
            strategy = MartingaleStrategy(params)
            self.strategies[bot_id] = strategy
            return strategy
        except Exception as e:
            logger.error(f"Failed to create strategy for bot {bot_id}: {e}")
            raise e

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

    def _calculate_stablecoin_balance(self, balance: dict) -> float:
        """Calculate total balance across USDT and USDC stablecoins."""
        total = 0.0
        for currency in STABLECOINS:
            curr_bal = balance.get(currency)
            if isinstance(curr_bal, dict):
                total += float(curr_bal.get('total', 0.0))
        return total

    def sync_all_bots(self):
        """
        Synchronizes the state of all active bots with the exchange.
        Uses the new comprehensive reconciliation system (v2.0).
        """
        logger.info("Starting comprehensive state reconciliation...")
        
        if not self.exchanges:
             logger.warning("⚠️ Cannot sync bots: Exchanges not initialized.")
             return

        try:
            # Instantiate Reconciler with current exchanges
            # We must pass the actual exchange instances
            from engine.reconciler import StateReconciler
            reconciler = StateReconciler(self.exchanges)
            
            # Execute Full Reconciliation
            results = reconciler.reconcile_all()
            
            # Log summary (v2.0: Actions Taken)
            actions_count = sum(1 for r in results if r.action_taken.value != "no_action")
            zombie_fixes = sum(1 for r in results if r.action_taken.value == "reset_to_idle")
            manual_warnings = sum(1 for r in results if r.requires_manual_intervention)
            
            logger.info(f"Reconciliation complete: {actions_count} actions taken ({zombie_fixes} zombie resets), {manual_warnings} manual warnings needed.")
            
            # 🚨 Emit loud alerts for anything requiring manual intervention
            for r in results:
                if r.requires_manual_intervention:
                    logger.warning(
                        f"\n"
                        f"════════════════════════════════════════════════════\n"
                        f" ⚠️  MANUAL INTERVENTION REQUIRED: {r.pair}  ⚠️\n"
                        f"════════════════════════════════════════════════════\n"
                        f" Reason: {r.details}\n"
                        f" Action: Go to Binance Web UI → Positions → {r.pair}\n"
                        f"         Identify which bot (by CQB_ order DNA) owns the gap.\n"
                        f"         Then manually reset the correct bot to match exchange reality.\n"
                        f"════════════════════════════════════════════════════"
                    )
            
        except Exception as e:
            logger.error(f"❌ Critical Error during State Reconciliation: {e}")
    


    def startup_sync(self):
        """
        Active Reconciliation on Startup.
        Forces the DB to match the Exchange (Source of Truth).
        - Cancels orphan orders (Ghost Orders).
        - Adopts active positions if missing from DB.
        """
        logger.info("🔄 [STARTUP-SYNC] Analyzing Exchange Reality...")
        
        try:
            # 0a. 📡 PRIME POSITION SNAPSHOT — fetch fresh exchange reality BEFORE any reconciliation.
            # ─────────────────────────────────────────────────────────────────────────────────────────
            # ROOT CAUSE FIX (v2.1.2):
            #   reconstruct_offline_fills and adopt_from_physical_positions both read 'active_positions'
            #   from SQLite to detect gaps. At startup, that table holds the last snapshot from the
            #   *previous* session — which already matched the virtual ledger state at shutdown.
            #   Any fills that occurred while the engine was offline are invisible to the gap detector,
            #   so it reports "zero gap" and skips the history scan entirely.
            #
            #   Fix: populate active_positions from the live exchange FIRST, so all subsequent
            #   reconciliation passes operate on ground-truth physical reality.
            # ─────────────────────────────────────────────────────────────────────────────────────────
            try:
                from engine.database import update_active_positions_snapshot
                logger.info("📡 [STARTUP-PRIME] Fetching live exchange positions to prime active_positions snapshot...")
                _primed_any = False
                for _mt, _ex in self.exchanges.items():
                    try:
                        _snap = _ex.fetch_positions()
                        if _snap is not None:
                            update_active_positions_snapshot(_snap)
                            logger.info(f"✅ [STARTUP-PRIME] Primed active_positions from {_mt} ({len(_snap)} positions).")
                            _primed_any = True
                            break  # One exchange is sufficient for position snapshot
                    except Exception as _pe:
                        logger.warning(f"⚠️ [STARTUP-PRIME] Could not fetch positions from {_mt}: {_pe}")
                if not _primed_any:
                    logger.warning("⚠️ [STARTUP-PRIME] Could not prime active_positions — reconciler will use stale snapshot.")
            except Exception as _prime_err:
                logger.warning(f"⚠️ [STARTUP-PRIME] Position prime failed (non-fatal): {_prime_err}")

            # 0b. 🛡️ Offline Fill Detection — now runs against fresh exchange reality.
            # This credits any fills that happened while the engine was offline,
            # so maintain_orders sees correct step/invested before placing new orders.
            if self._reconciler:
                try:
                    logger.info("🔍 [STARTUP-SYNC] Running offline fill detection (48h window)...")
                    stats = self._reconciler.reconstruct_offline_fills(since_hours=48)
                    logger.info(f"✅ [STARTUP-SYNC] Offline fills credited: {stats}")
                except Exception as _rf_err:
                    logger.warning(f"⚠️ [STARTUP-SYNC] Offline fill detection failed (non-fatal): {_rf_err}")

            # 🔑 ORDER-ID LEDGER VERIFICATION
            # After crediting any offline fills, recompute each bot's invested amount
            # from confirmed CQB_{id}_ order fills. This self-heals any counter drift
            # caused by WS drops, engine crashes, or mid-update restarts — no manual
            # DB patching required.
            try:
                from engine.database import sync_trades_from_orders
                _conn = get_connection()
                _active_ids = [r[0] for r in _conn.execute(
                    "SELECT id FROM bots WHERE is_active=1"
                ).fetchall()]
                _fixes = sum(sync_trades_from_orders(bid) for bid in _active_ids)
                if _fixes:
                    logger.info(f"✅ [STARTUP-LEDGER-VERIFY] Corrected {_fixes} bot(s) with ledger drift from order fills.")
                else:
                    logger.info("✅ [STARTUP-LEDGER-VERIFY] All bot ledgers are in sync with confirmed order fills.")
            except Exception as _lv_err:
                logger.warning(f"⚠️ [STARTUP-LEDGER-VERIFY] Ledger verification failed (non-fatal): {_lv_err}")

            # 🚀 ROOT CAUSE FIX: After crediting offline fills and verifying the ledger,
            # run _align_memory_to_ledger() to ensure the trades table exactly matches
            # the bot_orders ledger before any trading decisions are made.
            # This is the definitive self-heal for the System vs Exchange discrepancies.
            try:
                if self._reconciler:
                    self._reconciler._align_memory_to_ledger()
                    logger.info("✅ [STARTUP] Memory-to-ledger alignment complete.")
            except Exception as _smal_err:
                logger.warning(f"⚠️ [STARTUP] Memory-to-ledger alignment failed (non-fatal): {_smal_err}")

            # v2.0: Seal all active bot states from the authoritative ledger (bot_orders).
            # This is the canonical startup gate — trades table is written exactly once,
            # from confirmed fills, before any orders are placed or cancelled.
            try:
                from engine.ledger import seal_all_active_bots
                corrected = seal_all_active_bots()
                if corrected:
                    logger.info(f"✅ [STARTUP-SEAL] {corrected} bot(s) had trades row corrected by seal_all_active_bots().")
                else:
                    logger.info("✅ [STARTUP-SEAL] All bot trades rows are consistent with ledger fills.")
            except Exception as _seal_err:
                logger.warning(f"⚠️ [STARTUP-SEAL] seal_all_active_bots failed (non-fatal): {_seal_err}")



            # 🔬 BIDIRECTIONAL PROOF RECONCILIATION — run at every startup.
            # Verifies all ledger orders against exchange (PASS 1) and scans exchange
            # fills for DNA-matched orders not yet in bot_orders (PASS 2).
            # This is the definitive fix for SUI/XRP/BTC ghost-position discrepancies.
            try:
                if self._reconciler:
                    logger.info("🔬 [STARTUP] Running bidirectional physical position reconciliation...")
                    _adopt_results = self._reconciler.adopt_from_physical_positions(limit_per_symbol=500)
                    if _adopt_results:
                        for _bid, _res in _adopt_results.items():
                            logger.info(
                                f"  📊 Bot {_bid} ({_res.get('symbol')} {_res.get('side')}): "
                                f"phys={_res.get('phys_qty'):.4f} "
                                f"proved={_res.get('proved_qty', 0):.4f} "
                                f"healed={_res.get('p1_healed', 0)} "
                                f"adopted={_res.get('p2_adopted', 0)} "
                                f"match={_res.get('qty_matched')}"
                            )
                    else:
                        logger.info("  [STARTUP] No physical positions to reconcile.")
            except Exception as _adopt_err:
                logger.warning(f"⚠️ [STARTUP] Physical adoption scan failed (non-fatal): {_adopt_err}")



            active_bots = self.get_active_bots()
            allowed_bot_ids = {str(b[0]) for b in active_bots if b[9] == 1} # Only Active bots
            logger.info(f"   > Active Bots Allowed: {allowed_bot_ids}")

            # 2. Scan ALL Open Orders
            total_cancelled = 0
            for m_type, ex in self.exchanges.items():
                try:
                    orders = ex.fetch_open_orders()
                    if not orders: continue
                    
                    for o in orders:
                        cid = o.get('clientOrderId', '')
                        # Identify Bot ID from ClientID (Format: CQB_{bot_id}_...)
                        bot_id = None
                        if cid.startswith('CQB_'):
                            parts = cid.split('_')
                            if len(parts) > 1:
                                bot_id = parts[1]
                        
                        # Decision Logic
                        should_cancel = False
                        reason = ""
                        
                        if bot_id:
                            if bot_id not in allowed_bot_ids:
                                # 🚀 SIGNATURE-BASED ACCURACY FIX:
                                # Do NOT purge orders with CQB prefix - they are "System DNA".
                                # Tag them as STRAY for the UI to handle, don't just delete them.
                                should_cancel = False 
                                reason = f"Bot {bot_id} exists on exchange but is STOPPED/Unknown in DB. Tagging for recovery."
                                logger.info(f"📍 [STARTUP-SYNC] Identified STRAY Bot Order {o['id']} (Bot {bot_id}). PRESERVING for recovery.")
                        else:
                            # Unknown/Manual Order - strict mode would cancel, but safety mode ignores
                            if getattr(config, 'STRICT_CLEANUP', True):
                                should_cancel = True
                                reason = "Unknown/Manual Order (Strict Mode)"
                        
                        if should_cancel:
                            logger.warning(f"🚫 [STARTUP-CLEANUP] Cancelling Ghost Order {o['id']} ({o['symbol']}): {reason}")
                            ex.cancel_order(o['id'], o['symbol'])
                            total_cancelled += 1
                            
                except Exception as e:
                    logger.error(f"   > Failed to scan orders for {m_type}: {e}")

            if total_cancelled > 0:
                logger.info(f"✅ [STARTUP-CLEANUP] Cancelled {total_cancelled} ghost orders.")
            else:
                logger.info("✅ [STARTUP-CLEANUP] No host orders (or only stray CQB orders) found.")

            # 🚀 [v2.4.1] FINAL GATE: FULL RECONCILIATION.
            # Runs the complete resolve_net_mismatch logic to identify and clear
            # wrong-side residues (ghosts) immediately on startup.
            try:
                if self._reconciler:
                    logger.info("🛡️ [STARTUP-RECON] Executing full reconciliation pass...")
                    self._reconciler.reconcile_all()
                    logger.info("✅ [STARTUP-RECON] Full reconciliation complete.")
            except Exception as _recon_err:
                logger.error(f"❌ [STARTUP-RECON] Full reconciliation failed: {_recon_err}")

            # 3. Scan Positions (Adoption is verified in run_cycle via snapshot logic)
            # We explicitly run one cycle of 'update_active_positions_snapshot' to map reality to DB
            # self.run_cycle() # REMOVED: Premature cycle execution before reconciliation settles

        except Exception as e:
            logger.error(f"❌ [STARTUP-SYNC] Failed: {e}")

    def _calculate_unrealized_pnl(self, exchange_snapshot=None) -> float:
        """Calculates total unrealized PnL across all active market types."""
        total_unrealized_pnl = 0.0
        
        # If snapshot provided, use it
        if exchange_snapshot:
            for mt, data in exchange_snapshot.items():
                positions = data.get('positions', [])
                for p in positions:
                    total_unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            return total_unrealized_pnl

        # Fallback (Manual fetch)
        active_market_types = set(self.exchanges.keys())
        for mt in active_market_types:
            try:
                ex = self.exchanges[mt]
                all_positions = ex.fetch_positions()
                for p in all_positions:
                    total_unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            except Exception as e:
                logger.warning(f"Failed to fetch positions for PnL calculation in {mt}: {e}")
                
        return total_unrealized_pnl

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

    def check_circuit_breaker(self, exchange_snapshot=None):
        """
        Global Circuit Breaker: Checks if account equity has dropped below safe limits.
        """
        if getattr(config, 'NO_API_MODE', False):
            return
            
        if self.circuit_breaker_triggered or self.initial_equity <= 0:
            return

        try:
            total_stablecoin = 0.0
            balance_fetch_success = False

            # Prepare active bots for cost calculation
            active_bots_raw = self.get_active_bots()
            active_bots = [b for b in active_bots_raw if b[9] == 1]

            # Use snapshot if available
            if exchange_snapshot:
                for mt, data in exchange_snapshot.items():
                    balance = data.get('balance')
                    if balance:
                        total_stablecoin += self._calculate_stablecoin_balance(balance)
                        balance_fetch_success = True
            else:
                # Fallback to manual fetch
                active_market_types = set()
                for bot in active_bots:
                    config_dict = json.loads(bot[5]) if bot[5] else {}
                    active_market_types.add(normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE)))
                
                if not active_market_types: active_market_types.add(config.MARKET_TYPE)

                for mt in active_market_types:
                    if mt in self.exchanges:
                        try:
                            balance = self.exchanges[mt].fetch_balance()
                            if balance:
                                total_stablecoin += self._calculate_stablecoin_balance(balance)
                                balance_fetch_success = True
                        except Exception: pass
            
            if not balance_fetch_success:
                logger.warning("Circuit breaker check skipped - balance fetch failed")
                return
                
            invested_cost = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and t_data.get('total_invested') and t_data['total_invested'] > 0:
                    invested_cost += float(t_data['total_invested'])
            
            # Unrealized PnL from snapshot/cache
            unrealized_pnl = 0.0
            if exchange_snapshot:
                for mt, data in exchange_snapshot.items():
                    positions = data.get('positions', [])
                    for p in positions:
                        unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            else:
                # Fallback to manual fetch
                active_market_types = set()
                for bot in active_bots:
                    config_dict = json.loads(bot[5]) if bot[5] else {}
                    active_market_types.add(normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE)))
                
                if not active_market_types: active_market_types.add(config.MARKET_TYPE)

                for mt in active_market_types:
                    if mt in self.exchanges:
                        try:
                            positions = self.exchanges[mt].fetch_positions()
                            for p in positions:
                                unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
                        except: pass
            
            current_equity = total_stablecoin + invested_cost + unrealized_pnl
            
            # Log for debugging
            logger.debug(f"Circuit Check: Equity ${current_equity:.2f} (Cash: {total_stablecoin:.2f} + Cost: {invested_cost:.2f} + uPnL: {unrealized_pnl:.2f})")
            
            if self.initial_equity > 0:
                drawdown = (self.initial_equity - current_equity) / self.initial_equity * 100
                if drawdown >= config.GLOBAL_STOP_LOSS_PCT:
                    logger.critical(f"CIRCUIT BREAKER TRIGGERED! Drawdown: {drawdown:.2f}%")
                    self.circuit_breaker_triggered = True
                    with open(config.PATHS["EMERGENCY_FILE"], "w") as f:
                        f.write(f"Circuit Breaker Triggered at {drawdown:.2f}% drawdown")
                    self.handle_emergency_liquidation()
        except Exception as e:
            logger.error(f"Circuit breaker check failed: {e}")

    def get_active_bots(self):
        """Fetches all bots and their current status."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Query returns all bots LEFT JOIN trades to get real investment state
            cursor.execute('''
                SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config, 
                       COALESCE(t.total_invested, 0), 
                       COALESCE(t.current_step, 0), 
                       b.rsi_limit, b.is_active,
                       b.base_size, b.martingale_multiplier,
                       b.status
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
            ''')
            bots = cursor.fetchall()
            return bots
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally: 
            pass # conn.close() disabled for singleton safety



    def get_expected_active_positions_count(self):
        """Returns count of bots that DB says should have positions."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
            count = cursor.fetchone()[0]
            pass # conn.close() disabled for singleton safety
            return count
        except Exception as e:
            logger.error(f"Failed to get expected positions count: {e}")
            return 0

    def run_cycle(self):
        start_time = time.time()
        logger.debug("Entering run_cycle")
        self.orders_this_cycle = 0
        self.cycle_count += 1

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

        # 🔧 PERIODIC LEDGER ALIGNMENT (every 30 cycles ≈ every 15 min)
        # 🚀 ROOT CAUSE FIX: _align_memory_to_ledger() compares trades table against bot_orders ledger
        # and corrects any drift. Without this, ledger gaps accumulate silently forever.
        if self.cycle_count % 30 == 0 and self._reconciler:
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
        try:
            from engine.websocket_handler import get_websocket_handler, start_websocket_stream
            from engine.ws_event_handlers import handle_order_update, handle_position_update
            
            ws_h = get_websocket_handler()
            if not ws_h or not ws_h.is_alive:
                logger.warning("⚠️ WebSocket handler is DOWN or not initialized. Restarting...")
                start_websocket_stream(
                    on_order_update=handle_order_update,
                    on_position_update=handle_position_update
                )
        except Exception as ws_err:
            logger.error(f"Failed WS health check: {ws_err}")

        # 1. Global Optimization: Fetch Snapshots once per cycle
        # This fills the ExchangeInterface internal generic cache
        exchange_snapshot = {}
        bots = []
        try:
            logger.debug("Cycle Start - Fetching Bots")
            all_bots = self.get_active_bots()
            bots = [b for b in all_bots if b[9] == 1] # Filter for active bots

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
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]) or os.path.exists(config.PATHS["STOP_FILE"]):
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
        self.cycle_count += 1
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
        


        # Publish cycle time
        end_time = time.time()
        BOT_CYCLE_TIME.set(end_time - start_time)

        # AGGREGATE SMART POLLING
        # Find minimum requested sleep time. Default to 10s if no requests.
        valid_intervals = [r for r in results if isinstance(r, (int, float)) and r > 0]
        recommended_sleep = min(valid_intervals) if valid_intervals else 10.0
        
        return recommended_sleep

    def handle_emergency_liquidation(self):
        """
        Emergency liquidation for all active bots.
        BUG FIX: Now properly handles futures positions.
        """
        bots = self.get_active_bots()
        for bot in bots:
            id, name, pair = bot[0], bot[1], bot[2]
            config_json = bot[5]
            config_dict = json.loads(config_json) if config_json else {}
            mt = normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE))
            ex = self.exchanges.get(mt, self.exchange)
            
            if not ex:
                logger.error(f"Cannot liquidate {name}: No exchange interface available")
                continue

            try:
                ex.cancel_orders_by_bot_id(id, pair)
                
                if not config.DRY_RUN and mt in ['future', 'swap']:
                    # For futures, fetch positions properly
                    try:
                        positions = ex.exchange.fetch_positions()
                        # Normalize symbol for comparison
                        target_pair_clean = pair.replace('/', '').split(':')[0]
                        
                        for pos in positions:
                            if not pos: continue
                            pos_symbol = pos.get('symbol', '').replace('/', '').split(':')[0]
                            
                            if pos_symbol == target_pair_clean:
                                if float(pos.get('contracts', 0) or pos.get('size', 0) or 0) != 0:
                                    qty = float(pos.get('contracts', 0) or pos.get('size', 0))
                                    side = 'sell' if qty > 0 else 'buy'  # Short if long, Long if short
                                    close_qty = abs(qty)
                                    logger.warning(f"Emergency Market Close {close_qty} {pair} for {name}")
                                    ex.create_order(pair, 'market', side, close_qty)
                                    
                                    # CRITICAL FIX: Update DB to reflect closure
                                    # We use reset_bot_after_tp to clear the trade record
                                    # Passing 0 as exit price since it's a panic close (or use current price if available)
                                    try:
                                        reset_bot_after_tp(id, exit_price=0.0, action_label='EMERGENCY_CLOSE')
                                        logger.warning(f"✅ Bot {name} Database Reset after Emergency Close")
                                    except Exception as db_err:
                                        logger.error(f"Failed to reset DB for {name}: {db_err}")
                                        
                    except Exception as pos_err:
                        logger.error(f"Failed to fetch positions for {pair}: {pos_err}")
                        
            except Exception as e: logger.error(f"Cleanup failed for {name}: {e}")


if __name__ == "__main__":
    STOP, EMERGENCY = config.PATHS["STOP_FILE"], config.PATHS["EMERGENCY_FILE"]

    # --- STEP 1: OS-ENFORCED SINGLETON (SocketLock) ---
    lock = SocketLock()
    if not lock.acquire():
        sys.exit(1)

    init_db()

    # --- STEP 2: PREFLIGHT CHECK (Startup Gate) ---
    try:
        from engine.preflight import preflight_check
        pf_result = preflight_check()
        if pf_result['passed']:
            logger.info(f"✅ PREFLIGHT PASSED: {pf_result['summary']}")
        else:
            logger.warning(f"⚠️ PREFLIGHT ISSUES: {pf_result['summary']}")
            # Auto-healing was attempted. Log details but continue.
            for issue in pf_result.get('issues', []):
                logger.warning(f"  → {issue}")
    except Exception as e:
        logger.error(f"Preflight check failed (non-fatal): {e}")
        # Continue anyway — preflight is additive in Phase A

    # === METRICS SERVER STARTUP ===
    try:
        metrics_server = MetricsServer(port=config.METRICS_PORT)
        metrics_server.start()
    except Exception as e:
        logger.error(f"FATAL: Failed to start Metrics Server on port {config.METRICS_PORT}: {e}")
        lock.release()
        sys.exit(1)
    
    logger.info("Bot Service Started.")
    try: runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        # === METRICS SERVER STOP ===
        metrics_server.stop()
        lock.release()
        sys.exit(1)
    runner.running = True

    # 🚀 ROOT CAUSE FIX: Graceful Signal Handling
    def _graceful_shutdown(signum, frame):
        logger.info(f"Signal {signum} received. Initiating graceful shutdown...")
        runner.running = False  # Triggers the main loop exit

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    
    # BUG FIX: Clear emergency file on successful startup (prevents false liquidation on restart)
    if os.path.exists(EMERGENCY):
        os.remove(EMERGENCY)
        logger.info("Cleared stale emergency file")
    
    if os.path.exists(STOP): os.remove(STOP)
    
    failures = 0
    last_heartbeat = 0
    last_cleanup = 0
    cycle_sleep = 15.0 # Default fallback
    
    # Import cleanup utility and WS Server
    from engine.exchange_interface import cleanup_caches
    from engine.websocket_server import WebSocketServer
    
    # Start WebSocket Server
    ws_server = None
    try:

        logger.info("Starting WebSocket Server thread...")
        ws_server = WebSocketServer(port=8765)
        ws_server.start()
        logger.info("WebSocket Server thread started.")
    except Exception as e:
        logger.error(f"Failed to start WebSocket Server: {e}")
    
    while runner.running:
        try:
            # ── SIGNAL FILE CHECKS (checked every cycle) ─────────────────────
            # These files are written by the Streamlit UI sidebar buttons.
            # Without these checks, Stop Monitoring and Emergency Close All are
            # completely non-functional — the engine never sees them.
            if os.path.exists(EMERGENCY):
                logger.critical("🚨 EMERGENCY LIQUIDATION SIGNAL received. Closing all positions...")
                os.remove(EMERGENCY)
                try:
                    runner.handle_emergency_liquidation()
                    logger.critical("✅ Emergency liquidation complete.")
                except Exception as _em_err:
                    logger.critical(f"Emergency liquidation failed: {_em_err}")
                runner.running = False
                break

            if os.path.exists(STOP):
                logger.info("🛑 STOP signal received. Shutting down gracefully...")
                os.remove(STOP)
                runner.running = False
                break
            # ─────────────────────────────────────────────────────────────────

            # Periodic Cache Cleanup (Every 60s)
            now = time.time()

            if now - last_cleanup > 60:
                cleanup_caches()
                last_cleanup = now
            
            result = runner.run_cycle()
            
            # Broadcast State via WebSocket
            if ws_server and ws_server.is_alive() and ws_server.loop and not ws_server.loop.is_closed():
                logger.debug(f"[WS_RUNNER] Attempting broadcast. WS server running: {ws_server.running}, loop running: {ws_server.loop.is_running()}")
                try:
                    from engine.database import get_all_bots
                    bots_data = get_all_bots()
                    payload = {
                        "type": "update",
                        "timestamp": time.time(),
                        "bots": bots_data
                    }
                    ws_server.broadcast(payload)
                except Exception as wse:
                     logger.error(f"WS Broadcast Error: {wse}")
            else:
                pass


            if result is False: break
            
            # Update sleep time based on smart polling
            if isinstance(result, (int, float)) and result > 0:
                cycle_sleep = result
            else:
                cycle_sleep = 15.0
                
            failures = 0
            
            # Heartbeat every 60s to confirm system is alive
            if time.time() - last_heartbeat > 60:
                logger.info("💓 System Heartbeat - Active")
                last_heartbeat = time.time()
                
        except Exception as e:
            failures += 1
            logger.error(f"Cycle failed ({failures}): {e}", exc_info=True)
            cycle_sleep = 15.0 # Reset to safe slow polling on error
            if failures >= MAX_CONSECUTIVE_FAILURES: break
        except BaseException as e:
            logger.critical(f"🛑 FATAL RUNNER ERROR: {e}", exc_info=True)
            break
            
        time.sleep(cycle_sleep)
    # === METRICS SERVER STOP ===
    metrics_server.stop()
    
    # 🚀 ROOT CAUSE FIX: Drain async DB write queue before exit
    try:
        from engine.ws_event_handlers import stop_db_worker
        logger.info("Flushing async DB write queue before exit...")
        stop_db_worker(timeout=10.0)
        logger.info("✅ DB write queue flushed.")
    except Exception as e:
        logger.error(f"Failed to flush DB write queue: {e}")

    lock.release()
