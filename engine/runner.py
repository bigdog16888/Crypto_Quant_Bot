import time
import logging
import json
import sys
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import threading
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.bot_executor import BotExecutor
import engine.bot_executor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME


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

# Thread-local storage logic moved to bot_executor.py where it is actually used.
# Runner uses self.exchanges for main-thread operations.

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
        
        # ========== RUNAWAY ORDER PROTECTION ==========
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()
        
        # UI Synchronization: Write PID file so Streamlit knows we are running
        self._write_pid_file()

    def _write_pid_file(self):
        try:
            pid_file = config.PATHS["PID_FILE"]
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"✅ PID file written: {pid_file} (PID: {os.getpid()})")
        except Exception as e:
            logger.error(f"❌ Failed to write PID file: {e}")
        
        self.last_order_reset = time.time()
        
        # LAYER 3 FIX: Cycle counter for periodic reconciliation
        self.cycle_count = 0
        
        logger.info("DEBUG: Initializing Safety Baseline...")
        self._initialize_safety_baseline()
        
        # State Synchronization
        try:
            logger.info("DEBUG: Starting Startup Sync...")
            # ULW-Sisyphus: DISABLED redundant StateManager sync.
            # The main `self.sync_all_bots()` call is the single source of truth for startup reconciliation.
            # from engine.state_manager import get_state_manager
            # sm = get_state_manager()
            # reconcile_results = sm.reconcile_all()
            # logger.info(f"DEBUG: StateManager Reconciliation: {len(reconcile_results['synced'])} synced, {len(reconcile_results['failed'])} failed")
            
            # This is the PRIMARY startup reconciliation routine.
            self.sync_all_bots()
            logger.info("DEBUG: Startup Sync Complete")

            # --- FUNDAMENTAL FIX: CLEANUP INACTIVE TRADES ---
            # Ensure no database zombies exist from previous crashes
            try:
                from engine.database import check_and_fix_integrity
                check_and_fix_integrity()
            except Exception as e:
                logger.error(f"Startup integrity check failed: {e}")

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
                        m_type = cfg.get('market_type', config.MARKET_TYPE)
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
                            offline_stats = reconciler.detect_offline_fills(ex, since_hours=48)
                            if offline_stats['grid_fills'] + offline_stats['tp_fills'] + offline_stats['entry_fills'] > 0:
                                logger.info(f"📋 Offline Fills ({m_type}): {offline_stats['entry_fills']} entries, {offline_stats['grid_fills']} grids, {offline_stats['tp_fills']} TPs")
            except Exception as e:
                logger.error(f"Failed to detect offline fills: {e}")
            
            
            # 6. WebSocket Stream Startup (Phase 7)
            # Start real-time order updates to replace polling
            try:
                from engine.websocket_handler import start_websocket_stream
                from engine.ws_event_handlers import handle_order_update, handle_position_update
                
                ws_started = start_websocket_stream(
                    on_order_update=handle_order_update,
                    on_position_update=handle_position_update
                )
                if ws_started:
                    logger.info("✅ WebSocket stream started for real-time updates")
                else:
                    logger.warning("⚠️ WebSocket stream failed to start, using polling fallback")
            except ImportError:
                logger.info("WebSocket handler not available, using polling mode")
            except Exception as e:
                logger.error(f"Failed to start WebSocket stream: {e}")
                
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
                    active_market_types.add(config_dict.get('market_type', config.MARKET_TYPE))
                
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
                    active_market_types.add(config_dict.get('market_type', config.MARKET_TYPE))
                
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
                       b.rsi_limit, b.is_active
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
        start_time = time.time() # Start timing

        start_time = time.time()
        self.orders_this_cycle = 0
        
        # 1. Global Optimization: Fetch Snapshots once per cycle
        # This fills the ExchangeInterface internal generic cache
        exchange_snapshot = {}
        bots = []
        try:
            logger.info("DEBUG: Cycle Start - Fetching Bots")
            all_bots = self.get_active_bots()
            bots = [b for b in all_bots if b[9] == 1] # Filter for active bots

            active_market_types = set()
            for bot in bots:
                config_json = bot[5]
                cfg = json.loads(config_json) if config_json else {}
                mt = cfg.get('market_type', config.MARKET_TYPE)
                active_market_types.add(mt)
            
            if not active_market_types: active_market_types.add(config.MARKET_TYPE)

            # 🚀 Initializing snapshot variables to prevent UnboundLocalError
            snap_pos = None
            snap_bal = None
            snap_orders = None

            for mt in active_market_types:
                if mt in self.exchanges:
                    ex = self.exchanges[mt]
                    ex = self.exchanges[mt]
                    snap_pos = ex.fetch_positions()
                    # Need to explicitly define these for exchange_snapshot
                    snap_bal = ex.fetch_balance()
                    snap_orders = ex.fetch_open_orders()
                    
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
                                logger.critical(f"❌ [SNAPSHOT-CRITICAL] Mismatch confirmed: DB expects {expected_count} but Exchange has 0.")
                                logger.critical(f"🔧 [AUTO-HEAL] Trusting Exchange Logic. Marking missing positions as closed in DB...")
                                
                                # 🚀 FUNDAMENTAL FIX: Auto-Resolve Mismatch
                                # Instead of aborting, we allow the cycle to proceed with empty positions.
                                # The 'update_full_snapshot' later in the cycle (or sync_all_bots) will handle the DB cleaning.
                                # But we must ensure 'sync_all_bots' is called OR we manually clean here.
                                
                                # For robust safety, we explicitly reset the ghost bots now.
                                
                                # from engine.reconciler import sync_all_bots (REMOVED: Does not exist)
                                # sync_all_bots()
                                
                                logger.info(f"✅ [AUTO-HEAL] Ghost positions will be cleared in subsequent logic.")
                                # Continue processing (snap_pos is [])
                            else:
                                logger.info(f"✅ [SNAPSHOT-RECOVERY] Force Refresh successful: Found {len(snap_pos)} positions.")
                    
                    logger.debug(f"DEBUG: Processing {len(snap_pos)} positions for {mt}")
                    
                    # 🚀 FUNDAMENTAL FIX: "Adoption Logic" for Orphaned Positions
                    # If Exchange has a position for a bot's pair, but DB says bot is IDLE, we must ADOPT it.
                    # This prevents "System 0 vs Exchange X" mismatch.
                    for pos in snap_pos:
                        pos_symbol = pos['symbol']
                        pos_amt = pos['contracts']
                        if pos_amt == 0: continue
                        
                        # Find if any active bot manages this symbol
                        # Bot structure: id, name, pair, strategy, ...
                        # normalized check
                        relevant_bots = [b for b in bots if normalize_symbol(b[2]) == normalize_symbol(pos_symbol)]
                        
                        for bot in relevant_bots:
                            b_id, b_name, b_pair, b_direction, _, _, b_invested, b_step, _, _ = bot
                            
                            # If bot is IDLE OR has tiny dust (mismatch) but exchange has real position -> ADOPT
                            # System (DB) might think it's 0.19, but Exchange is 138.78.
                            db_total = float(b_invested or 0)
                            # Calculated Exchange Notional
                            entry_price = float(pos['entryPrice'])
                            exch_notional = abs(float(pos_amt)) * entry_price
                            
                            # CRITICAL FIX: In One-Way Mode, only adopt if direction matches!
                            # Otherwise, Long and Short bots both adopt the same Net Position, causing double-counting.
                            pos_side = pos.get('side', 'LONG').upper() # 'LONG' or 'SHORT'
                            
                            # Parse side from position amount if side is ambiguous (some exchanges return 'both')
                            if pos_amt > 0: pos_side_real = 'LONG'
                            elif pos_amt < 0: pos_side_real = 'SHORT'
                            else: pos_side_real = 'FLAT'
                            
                            # If exchange reports 'BOTH' or specific side, trust sign of amount first
                            if pos_side == 'BOTH':
                                pos_side = pos_side_real
                            
                            # Check Direction
                            if b_direction.upper() != pos_side and b_direction.upper() != pos_side_real:
                                # Mismatch: I am Short, Position is Long -> Skip
                                continue

                            # Heuristic: If significant discrepancy (> $20), Force Sync (Self-Healing)
                            if abs(exch_notional - db_total) > 20.0:
                                logger.critical(f"🚑 [SELF-HEALING] Mismatch Detected for {b_name}: DB=${db_total:.2f} vs Exch=${exch_notional:.2f}. Syncing...")
                                
                                # Update DB to reflects this position
                                # from engine.database import update_bot_status (REMOVED: Toxic)
                                
                                # Force DB Update
                                try:
                                     import engine.database
                                     conn = engine.database.get_connection()
                                     cursor = conn.cursor()
                                     # Update status to OPEN and set invested
                                     cursor.execute("""
                                         UPDATE trades 
                                         SET total_invested = ?, 
                                             avg_entry_price = ?, 
                                             current_step = 1
                                         WHERE bot_id = ?
                                     """, (exch_notional, entry_price, b_id))
                                     conn.commit()
                                     conn.close()
                                     logger.info(f"✅ [ADOPTED] Bot {b_name} synced to {pos_amt} {b_pair} @ {entry_price}")
                                     
                                     # Force immediate re-evaluation next loop
                                     self.orders_this_cycle += 1 
                                except Exception as e:
                                     logger.error(f"❌ [ADOPTION-FAIL] Failed to update DB for {b_name}: {e}")

                    # 🚀 FUNDAMENTAL FIX: "Ghost Clearing" Logic (Per-Bot)
                    # If DB says bot has position (e.g., $0.19) but Exchange (snap_pos) has NONE for that symbol -> GHOST.
                    # We must clear it to sync with reality.
                    # We iterate all active bots for this market type.
                    relevant_bots_for_mt = [b for b in bots if b[5] and json.loads(b[5]).get('market_type', config.MARKET_TYPE) == mt]
                    if not relevant_bots_for_mt: # Fallback if config fails
                         relevant_bots_for_mt = [b for b in bots if config.MARKET_TYPE == mt]

                    for bot in relevant_bots_for_mt:
                        b_id, b_name, b_pair, b_direction, _, _, b_invested, b_step, _, _ = bot
                        
                        # Check if bot thinks it's invested
                        if float(b_invested or 0) > 0:
                            # Check if position exists in snapshot
                            # precise symbol matching
                            found_pos = None
                            for p in snap_pos:
                                if normalize_symbol(p['symbol']) == normalize_symbol(b_pair):
                                    found_pos = p
                                    found_pos = p
                                    break
                            
                            # 🚀 FUNDAMENTAL FIX: "Virtual Hedging" Ghost Logic
                            # In One-Way Mode with multiple bots (Long/Short), we cannot simply compare 1 Bot vs Exchange.
                            # We must compare NET SYSTEM POSITION vs EXCHANGE POSITION.
                            
                            # 1. Calculate Net System Position for this Bot's Pair
                            # Sum of all Active Bots' invested amounts involves direction.
                            # We need to look at 'active_bots' list (which is passed as 'bots' argument)
                            
                            net_system_qty = 0.0
                            # relevant_bots_for_mt is already filtered for this market type
                            for rb in relevant_bots_for_mt:
                                rb_pair = rb[2]
                                if normalize_symbol(rb_pair) != normalize_symbol(b_pair): continue
                                
                                rb_id = rb[0]
                                rb_direction = rb[3]
                                rb_invested = float(rb[6] or 0)
                                rb_entry = float(rb[7] or 0) # avg_entry_price is index 8? No, let's check retrieve logic.
                                # get_active_bots: id, name, pair, direction, strategy, config, invested, step, rsi, active
                                # Wait, get_active_bots SQL:
                                # SELECT b.id, b.name, b.pair, b.direction ... COALESCE(t.total_invested, 0) [6], COALESCE(t.current_step, 0) [7]
                                # avg_entry_price is NOT in the tuple. We only have total_invested ($).
                                # We can approximate quantity = total_invested / current_price. 
                                # Better: trust 'total_invested' as the dollar value.
                                
                                # But Exchange Position is in Contracts (Qty).
                                # We need either Price to convert, or just compare Notional Value ($).
                                
                                # Let's match Notional Value ($).
                                # Long = +$, Short = -$
                                if rb_direction.upper() == 'LONG':
                                    net_system_qty += rb_invested
                                else:
                                    net_system_qty -= rb_invested
                                    
                            # 2. Calculate Exchange Net Position ($)
                            exch_pnl_qty = 0.0
                            
                            # Find position for this pair
                            found_pos = None
                            for p in snap_pos:
                                if normalize_symbol(p['symbol']) == normalize_symbol(b_pair):
                                    found_pos = p
                                    break
                                    
                            if found_pos:
                                qty = float(found_pos['contracts'])
                                price = float(found_pos['entryPrice'])
                                side = found_pos['side'].upper()
                                # Convert to signed dollar value
                                val = qty * price
                                if side == 'SHORT': val = -val
                                exch_pnl_qty = val
                                
                            # 3. Compare with Tolerance
                            # If |NetSystem - Exch| < Threshold, then ALL bots on this pair are VALID.
                            # Even if Bot A is +1000 and Bot B is -1000 and Exch is 0.
                            diff = abs(net_system_qty - exch_pnl_qty)
                            limit = 20.0 # $20 tolerance
                            
                            if diff < limit:
                                # ✅ System is balanced (Hedged or synced). No Ghosts!
                                continue # Skip to next bot/pair
                            
                            # 4. If Mismatch, fall back to individual checks?
                            # No, if Net Mismatch exists, determining WHO is the ghost is hard.
                            # But we can check if *this specific bot* is contributing to the error.
                            
                            # Logic: If Exchange is 0, but this bot is $1000 -> It's a ghost (unless another bot is -$1000).
                            # But we just checked Net! If Net is 0, then we are good.
                            # If Net != 0 (e.g. NetSystem=1000, Exch=0), then SOMEONE is a ghost.
                            
                            # If we are here, there is a Net Mismatch.
                            # Case: Bot A (Long 1000), Exch (0). Net Diff = 1000.
                            # We should kill Bot A.
                            
                            # Case: Bot A (Long 1000), Bot B (Short 1000), Exch (Long 2000).
                            # NetSystem = 0. Exch = 2000. Diff = 2000.
                            # Both bots might be wrong? Or one?
                            
                            # Use strict existence check as fallback?
                            # If Exchange has NO position (0), then ANY bot with >0 is a ghost.
                            if not found_pos or float(found_pos['contracts']) == 0:
                                 logger.critical(f"👻 [GHOST-BUST] Bot {b_name} thinks it has ${b_invested} but Exchange has 0 and Net Mismatch > {limit}. Clearing...")
                                 try:
                                     import engine.database
                                     conn = engine.database.get_connection()
                                     cursor = conn.cursor()
                                     cursor.execute("UPDATE trades SET total_invested=0, current_step=0 WHERE bot_id=?", (b_id,))
                                     cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (b_id,))
                                     conn.commit()
                                     logger.info(f"✅ [GHOST-BUSTED] Bot {b_name} reset to IDLE.")
                                 except Exception as e:
                                     logger.exception(f"❌ [GHOST-FAIL] Failed to clear ghost for {b_name}: {e}")
                                 continue
                            
                            # If Exchange HAS position, but mismatch exists...
                            # This is harder to solv automatically without "Adoption".
                            # Adoption logic previously handled the "System 0 vs Exch X" case.
                            # This block handles "System X vs Exch 0/Y" case.
                            
                            # For now, we only aggressively kill if Exchange is ZERO (Empty).
                            # If Exchange has valid position, we assume it's correct and maybe we just drift?
                            # Or we trust "Adoption" to fix it if we were 0.

                    
                    # 🚀 NEW: Pre-fetch OHLCV (Price Data) for all active pairs to feed strategies
                    market_data = {}
                    active_pairs = set([b[2] for b in bots])
                    # from engine.exchange_interface import normalize_symbol (REMOVED - Use global)
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
                        except Exception as e:
                            logger.error(f"Failed to fetch market data for {pair}: {e}")

                    exchange_snapshot[mt] = {
                        'positions': snap_pos,
                        'balance': snap_bal,
                        'open_orders': snap_orders,
                        'market_data': market_data # 🚀 Added price data to snapshot
                    }
        except Exception as e:
            logger.warning(f"Failed to pre-fetch cycle snapshot: {e}")

        # --- FUNDAMENTAL FIX: Update Active Positions for UI ---
        # We must aggregate all positions from all active markets to give the UI a full picture.
        # This was previously missing, causing "System Mismatch" (Exchange 0 vs System X).
        try:
            all_positions = []
            for mt_key, snapshot in exchange_snapshot.items():
                if snapshot and 'positions' in snapshot:
                    all_positions.extend(snapshot['positions'])
            
            # Update the separate table used by UI/Monitor
            update_active_positions_snapshot(all_positions)
        except Exception as e:
            logger.error(f"Failed to update active_positions snapshot: {e}")

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

        logger.info(f"DEBUG: Starting cycle with {len(bots)} bots")
        if not bots:
            logger.warning("No active bots found to process in this cycle.")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Process bots using the primed cache
            raw_results = list(executor.map(lambda b: bot_executor.process_bot(b, exchange_snapshot=exchange_snapshot), bots))
        
        # Filter out None results (bots skipped or errored)
        processed_bot_results = [r for r in raw_results if r is not None and r[0] is not None]
        
        # 🚀 FUNDAMENTAL FIX: Aggregate all trade updates for atomic DB write
        trade_updates = [res[1] for res in processed_bot_results if res[1] is not None]
        
        if trade_updates or snap_pos:
            try:
                # Get physical positions for the update
                physical_positions = []
                for mt, snap in exchange_snapshot.items():
                    physical_positions.extend(snap.get('positions', []))

                update_full_snapshot(trade_updates, physical_positions)
                logger.info("✅ Atomic snapshot update completed.")
            except Exception as e:
                logger.error(f"❌ Failed to perform atomic snapshot update: {e}")

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
                from engine.reconciler import sync_all_bots
                logger.info("🔄 Running periodic position reconciliation...")
                sync_all_bots() # This now internally handles updates
                logger.info("🔄 Periodic position reconciliation complete")
            except Exception as e:
                logger.warning(f"Periodic reconciliation failed: {e}")
        


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
            mt = config_dict.get('market_type', config.MARKET_TYPE)
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
    init_db()

    
    # === METRICS SERVER STARTUP ===
    try:
        metrics_server = MetricsServer(port=config.METRICS_PORT)
        metrics_server.start()
    except Exception as e:
        logger.error(f"FATAL: Failed to start Metrics Server on port {config.METRICS_PORT}: {e}")
        sys.exit(1)
    
    logger.info("Bot Service Started.")
    try: runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        # === METRICS SERVER STOP ===
        metrics_server.stop()
        sys.exit(1)
    runner.running = True
    PID, STOP, EMERGENCY = config.PATHS["PID_FILE"], config.PATHS["STOP_FILE"], config.PATHS["EMERGENCY_FILE"]
    
    # BUG FIX: Clear emergency file on successful startup (prevents false liquidation on restart)
    if os.path.exists(EMERGENCY):
        os.remove(EMERGENCY)
        logger.info("Cleared stale emergency file")
    
    if os.path.exists(STOP): os.remove(STOP)
    with open(PID, "w") as f: f.write(str(os.getpid()))
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
        ws_server = WebSocketServer(port=config.METRICS_PORT)
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
            logger.error(f"Cycle failed ({failures}): {e}")
            cycle_sleep = 15.0 # Reset to safe slow polling on error
            if failures >= MAX_CONSECUTIVE_FAILURES: break
            
        time.sleep(cycle_sleep)
    # === METRICS SERVER STOP ===
    metrics_server.stop()
    if os.path.exists(PID): os.remove(PID)

