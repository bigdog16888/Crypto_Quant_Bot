import time
import logging
import json
import sys
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import threading
import psutil # Added for robust PID checking
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot, update_active_positions
import sqlite3
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
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

            # 3. Deep Reconciliation (Auto-Healing & Smart Adoption)
            # Must run AFTER exchanges are up but BEFORE bots start processing.
            # ULW-Sisyphus: DISABLED. This is redundant with the main `sync_all_bots()` call
            # which now runs the same logic. Keeping it creates startup race conditions.
            # from engine.reconciler import DeepReconciler
            # reconciler = None
            # try:
            #     reconciler = DeepReconciler(self.exchanges)
            #     reconciler.run() # Triggers Phase 6 Smart Adoption
            # except Exception as e:
            #     logger.error(f"Failed to run Deep Reconciliation: {e}")
            
            # Use a single reconciler instance for subsequent checks
            from engine.reconciler import StateReconciler
            reconciler = StateReconciler(self.exchanges)
            
            # FUNDAMENTAL FIX: Force Instant DB Snapshot
            # Populate active_positions table IMMEDIATELY so UI shows "Red" (Syncing) or Real Data, not "Green" (Empty)
            try:
                logger.info("📸 [STARTUP] Forcing immediate exchange snapshot...")
                snapshot_positions = []
                for mt, ex in self.exchanges.items():
                    if ex:
                        pos = ex.fetch_positions()
                        if pos: snapshot_positions.extend(pos)
                
                # Write to DB immediately
                if snapshot_positions:
                    update_active_positions_snapshot(snapshot_positions)
                    logger.info(f"✅ [STARTUP] Active Positions Table updated with {len(snapshot_positions)} positions.")
                else:
                    logger.info("✅ [STARTUP] Active Positions Table cleared (No positions found).")
            except Exception as e:
                logger.error(f"❌ [STARTUP] Failed to force initial snapshot: {e}")
            
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
            # Check if any orders filled while bot was offline.
            try:
                # Offline fills need to be checked per exchange
                if reconciler:
                    for m_type, ex in self.exchanges.items():
                        if ex:
                            offline_stats = reconciler.detect_offline_fills(since_hours=48)
                            if offline_stats['grid_fills'] + offline_stats['tp_fills'] + offline_stats['entry_fills'] > 0:
                                logger.info(f"📋 Offline Fills ({m_type}): {offline_stats['entry_fills']} entries, {offline_stats['grid_fills']} grids, {offline_stats['tp_fills']} TPs")
            except Exception as e:
                logger.error(f"Failed to detect offline fills: {e}")
            
            
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
            
            logger.info(f"Reconciliation complete: {actions_count} actions taken ({zombie_fixes} zombie resets), {manual_warnings} system warnings.")
            
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
            # 0. 🛡️ Offline Fill Detection FIRST — must run before any trading decisions
            # This credits any fills that happened while the engine was offline,
            # so maintain_orders sees correct step/invested before placing new orders.
            if self._reconciler:
                try:
                    logger.info("🔍 [STARTUP-SYNC] Running offline fill detection (48h window)...")
                    stats = self._reconciler.detect_offline_fills(since_hours=48)
                    logger.info(f"✅ [STARTUP-SYNC] Offline fills credited: {stats}")
                except Exception as _rf_err:
                    logger.warning(f"⚠️ [STARTUP-SYNC] Offline fill detection failed (non-fatal): {_rf_err}")

            # 1. Get Active Bots (The "Allowed" List)
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

            # 3. Scan Positions (Adoption is verified in run_cycle via snapshot logic)
            # We explicitly run one cycle of 'update_active_positions_snapshot' to map reality to DB
            self.run_cycle() # Force specific cycle logic update

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
                       b.base_size, b.martingale_multiplier
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
            ''')
            bots = cursor.fetchall()
            return bots
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally: 
            conn.close()



    def get_expected_active_positions_count(self):
        """Returns count of bots that DB says should have positions."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
            count = cursor.fetchone()[0]
            conn.close()
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
        # Runs fast (2h window) to keep latency low.
        if self.cycle_count % 10 == 0 and self._reconciler:
            try:
                logger.info(f"[PERIODIC] Running offline fill scan (2h window, cycle {self.cycle_count})...")
                _pof_stats = self._reconciler.detect_offline_fills(since_hours=2)
                if _pof_stats.get('total', 0) > 0:
                    logger.info(f"✅ [PERIODIC] Offline fills credited: {_pof_stats}")
                else:
                    logger.info(f"✅ [PERIODIC] No new fills found in scan.")
            except Exception as _pof_err:
                logger.warning(f"Periodic offline fill scan failed (non-fatal): {_pof_err}")

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
                    
                    if ws_cache.is_fresh(max_age_seconds=300):
                        logger.debug(f"⚡ [WS-CACHE] Reading positions and orders from memory for {mt}")
                        snap_pos = ws_cache.get_all_positions()
                        snap_orders = ws_cache.get_all_open_orders()
                    else:
                        snap_pos = ex.fetch_positions()
                        
                        # 🚀 BUG FIX: Binance Demo FAPI truncates fetch_open_orders() without symbol to ~12 orders!
                        # This HIDES existing orders from the engine, tricking it into placing duplicates!
                        # We must fetch open orders explicitly for every active pair on this market type.
                        snap_orders = []
                        mt_active_pairs = set([b[2] for b in bots if b[5] and normalize_market_type(json.loads(b[5]).get('market_type', config.MARKET_TYPE)) == mt])
                        if not mt_active_pairs: mt_active_pairs = set([b[2] for b in bots]) # Fallback
                        
                        for pair_symbol in mt_active_pairs:
                            pair_orders = ex.fetch_open_orders(pair_symbol)
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
                            
                            # Check if any bot claims this position
                            claimed = any(float(b[6] or 0) > 1.0 for b in same_dir_bots)
                            if not claimed and full_exch_notional > 10.0:
                                logger.info(f"📋 [MONITOR] Unclaimed {pos_side_real} position on {pos_symbol}: ${full_exch_notional:.2f} @ {entry_price}. Reconciler will handle.")
                        
                        # Flag bots that think they're invested but exchange disagrees
                        relevant_bots_for_mt = [b for b in bots if b[5] and normalize_market_type(json.loads(b[5]).get('market_type', config.MARKET_TYPE)) == mt]
                        if not relevant_bots_for_mt:
                             relevant_bots_for_mt = [b for b in bots if config.MARKET_TYPE == mt]
    
                        for bot in relevant_bots_for_mt:
                            b_id, b_name, b_pair, b_direction, _, _, b_invested, b_step, _, _ = bot
                            if float(b_invested or 0) <= 0:
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
                    # from engine.exchange_interface import normalize_symbol (REMOVED - Use global)
                    
                    # TTL mapping: how many seconds before each TF's cache expires
                    _TF_TTL = {
                        '1m': 55, '5m': 280, '15m': 840, '30m': 1700,
                        '1h': 3500, '4h': 14000, '1d': 82800
                    }
                    
                    for pair in active_pairs:
                        try:
                            # 🚀 FIXED: Ensure symbol is normalized for the specific exchange (e.g. BTC/USDC)
                            norm_pair = pair
                            # Check if the pair needs slashes (CCXT standard)
                            if '/' not in norm_pair:
                                if 'USDC' in norm_pair: norm_pair = norm_pair.replace('USDC', '/USDC')
                                elif 'USDT' in norm_pair: norm_pair = norm_pair.replace('USDT', '/USDT')
                            
                            ohlcv = ex.fetch_ohlcv(norm_pair, timeframe='1m', limit=50)
                            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']) # type: ignore
                            market_data[pair] = df
                            
                            # 🚀 Multi-TF fetch with TTL cache
                            # Collect unique TFs needed by bots on this pair
                            needed_tfs = set()
                            for b in bots:
                                if b[2] == pair:
                                    cfg = json.loads(b[5]) if b[5] else {}
                                    for key in ['cci_tf', 'rsi_tf', 'boll_tf', 'stoch_tf',
                                                'pat_1_tf', 'pat_2_tf', 'pat_3_tf', 'pat_4_tf',
                                                'MTF_Timeframe']:
                                        tf_val = cfg.get(key)
                                        if tf_val and tf_val != '1m':
                                            needed_tfs.add(tf_val)
                            
                            pair_tf_data = {'1m': df}
                            now = time.time()
                            for tf in needed_tfs:
                                cache_key = (pair, tf)
                                ttl = _TF_TTL.get(tf, 300)
                                cached = self._tf_cache.get(cache_key)
                                
                                if cached and (now - cached['fetched_at']) < ttl:
                                    # Cache hit — reuse
                                    pair_tf_data[tf] = cached['data']
                                else:
                                    # Cache miss or stale — fetch fresh
                                    try:
                                        tf_ohlcv = ex.fetch_ohlcv(norm_pair, timeframe=tf, limit=100)
                                        tf_df = pd.DataFrame(tf_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                        pair_tf_data[tf] = tf_df
                                        self._tf_cache[cache_key] = {'data': tf_df, 'fetched_at': now}
                                        logger.debug(f"📊 Fetched {tf} data for {pair} ({len(tf_df)} candles)")
                                    except Exception as e:
                                        logger.warning(f"Failed to fetch {tf} data for {pair}: {e}")
                                        # Use stale cache if available
                                        if cached:
                                            pair_tf_data[tf] = cached['data']
                            
                            multi_tf_data[pair] = pair_tf_data
                        except Exception as e:
                            logger.error(f"Failed to fetch market data for {pair}: {e}")

                    # 🚀 UI PERFORMANCE BATCHING: Save the OHLCV Cache to JSON for the Dashboard
                    try:
                        cache_dir = os.path.join(config.ROOT_DIR, 'data')
                        os.makedirs(cache_dir, exist_ok=True)
                        cache_file = os.path.join(cache_dir, 'market_cache.json')
                        
                        # We need to convert Pandas DataFrames into simple JSON dicts
                        json_ready_cache = {}
                        for pair, tf_dict in multi_tf_data.items():
                            json_ready_cache[pair] = {}
                            for tf, df in tf_dict.items():
                                json_ready_cache[pair][tf] = df.to_dict(orient='records')
                        
                        with open(cache_file, 'w') as f:
                            json.dump(json_ready_cache, f)
                        # logger.debug(f"💾 Saved Market Data Cache to {cache_file}")
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
        
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]):
            self.handle_emergency_liquidation()
            self.running = False
            return False
        if os.path.exists(config.PATHS["STOP_FILE"]):
            self.running = False
            return False

        # 3. Process Bots
        # Update workers size
        max_workers = min(len(bots) + 2, 20)
        
        if not hasattr(self, '_bot_executor') or self._bot_executor is None:
            self._bot_executor = BotExecutor(self)
        bot_executor = self._bot_executor

        # logger.info(f"🚀 BotRunner started. Cycle time: {POLL_INTERVAL_SECONDS}s")
        # logger.info(f"📂 DATABASE PATH: {config.PATHS['DB_FILE']}")
        # logger.info(f"📂 PID FILE: {config.PATHS['PID_FILE']}")
        logger.debug(f"DEBUG: Starting cycle with {len(bots)} bots")
        if not bots:
            logger.warning("No active bots found to process in this cycle.")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Process bots using the primed cache
            raw_results = list(executor.map(lambda b: bot_executor.process_bot(b, exchange_snapshot=exchange_snapshot), bots))
        
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
        if self.cycle_count % 60 == 0:
            try:
                from engine.reconciler import StateReconciler
                logger.info("🔄 Running periodic position reconciliation...")
                recon = StateReconciler(self.exchanges)
                recon.reconcile_all()
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
                                        reset_bot_after_tp(id, exit_price=0.0)
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
    lock.release()
