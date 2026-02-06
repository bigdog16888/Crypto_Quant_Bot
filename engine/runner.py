import time
import logging
import json
import sys
import os
from concurrent.futures import ThreadPoolExecutor
import threading
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.bot_executor import BotExecutor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME
from engine.reconciler import sync_all_bots
from engine.ownership import (
    init_ownership_tables, OwnershipState, OwnershipEvent,
    claim_ownership, become_passenger, handle_position_closed,
    check_first_claim_policy, reconcile_pair, get_pair_ownership,
    get_ownership_state, update_ownership_state
)
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
    maxBytes=50 * 1024 * 1024, # 50MB
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
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Thread-local storage logic moved to bot_executor.py where it is actually used.
# Runner uses self.exchanges for main-thread operations.

class BotRunner:
    def __init__(self):
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
            else:
                logger.critical("NO EXCHANGES INITIALIZED! Engine cannot run.")
                sys.exit(1)
        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}
        
        # Safety / Circuit Breaker State
        self.initial_equity = 0.0
        self.circuit_breaker_triggered = False
        
        # ========== RUNAWAY ORDER PROTECTION ==========
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()
        
        self.last_order_reset = time.time()
        
        # LAYER 3 FIX: Cycle counter for periodic reconciliation
        self.cycle_count = 0
        
        logger.info("DEBUG: Initializing Safety Baseline...")
        self._initialize_safety_baseline()
        
        # State Synchronization
        try:
            logger.info("DEBUG: Starting Startup Sync...")
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
            from engine.reconciler import DeepReconciler
            reconciler = None
            try:
                reconciler = DeepReconciler(self.exchanges)
                reconciler.run() # Triggers Phase 6 Smart Adoption
            except Exception as e:
                logger.error(f"Failed to run Deep Reconciliation: {e}")
            
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
        Uses the new comprehensive reconciliation system.
        """
        logger.info("Starting comprehensive state reconciliation...")
        results = sync_all_bots()
        
        # Log summary
        owner_count = sum(1 for r in results if r.position_owner.value == "owner")
        passenger_count = sum(1 for r in results if r.position_owner.value == "passenger")
        orphan_count = sum(1 for r in results if r.requires_manual_intervention)
        
        logger.info(f"Reconciliation complete: {owner_count} owners, {passenger_count} passengers, {orphan_count} require manual review")
    

    def _reconcile_ownership(self):
        """
        Ensures ownership state matches reality.
        """
        # logger.debug("Starting ownership reconciliation...")
        try:
            # 1. Get all active pairs from DB
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1")
            pairs = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if not pairs:
                return

            # 2. Bulk fetch positions from exchange to know truth
            has_position_map = {}
            if self.exchange:
                try:
                    # Use exchange_interface wrapper method for batching
                    all_positions = self.exchange.fetch_positions()
                    for p in all_positions:
                        size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if size != 0:
                            has_position_map[p['symbol']] = True
                except Exception as e:
                    logger.error(f"Reconciliation halted: Failed to fetch positions: {e}")
                    return
            else:
                logger.warning("No default exchange initialized. Skipping position fetch for ownership reconciliation.")
                return


            # 3. Reconcile each pair
            for pair in pairs:
                # logger.debug(f"Reconciling ownership for {pair}...")
                
                # Check if position exists on exchange
                exchange_has_pos = has_position_map.get(pair, False)
                
                # Pass BOOLEAN as required by reconcile_pair signature
                reconcile_pair(pair, exchange_has_pos)
                
        except Exception as e:
            logger.error(f"Ownership reconciliation failed: {e}")
        # logger.debug("Ownership reconciliation finished.")


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
                if t_data and len(t_data) > 3:
                    invested_sum += float(t_data[3])
            
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
                if t_data and len(t_data) > 3 and t_data[3] > 0:
                    invested_cost += float(t_data[3])
            
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
            # Query returns all bots
            cursor.execute('''
                SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active
                FROM bots 
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally: conn.close()



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
            bots = self.get_active_bots()

            active_market_types = set()
            for bot in bots:
                config_json = bot[5]
                cfg = json.loads(config_json) if config_json else {}
                active_market_types.add(cfg.get('market_type', config.MARKET_TYPE))
            
            if not active_market_types: active_market_types.add(config.MARKET_TYPE)

            for mt in active_market_types:
                if mt in self.exchanges:
                    ex = self.exchanges[mt]
                    # These calls are now cached for 3s
                    logger.info(f"DEBUG: Fetching Snapshot for {mt}")
                    snap_pos = ex.fetch_positions()
                    logger.info(f"DEBUG: Fetching Balance for {mt}")
                    snap_bal = ex.fetch_balance()
                    logger.info(f"DEBUG: Fetching Open Orders for {mt}")
                    snap_orders = ex.fetch_open_orders() # Bulk fetch all open orders
                    
                    exchange_snapshot[mt] = {
                        'positions': snap_pos,
                        'balance': snap_bal,
                        'open_orders': snap_orders
                    }
        except Exception as e:
            logger.warning(f"Failed to pre-fetch cycle snapshot: {e}")

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

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Process bots using the primed cache
            results = list(executor.map(lambda b: bot_executor.process_bot(b, exchange_snapshot=exchange_snapshot), bots))
        
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
                reconciler = StateReconciler(self.exchanges)
                for mt, ex in self.exchanges.items():
                    if ex:
                        reconciler._reconcile_positions(mt, ex)
                logger.info("🔄 Periodic position reconciliation complete")
            except Exception as e:
                logger.warning(f"Periodic reconciliation failed: {e}")
        
        # Ownership reconciliation: Check for owner failover and stale ownerships
        # logger.debug("Starting ownership reconciliation...")
        self._reconcile_ownership()
        # logger.debug("Ownership reconciliation complete.")
        
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
                ex.cancel_all_orders(pair)
                
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
    init_ownership_tables()  # Initialize ownership tracking tables
    
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
        ws_server = WebSocketServer(port=8765)
        ws_server.start()
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
            if ws_server:
                try:
                    from engine.database import get_all_bots
                    bots = get_all_bots()
                    # Serialize and broadcast
                    payload = {
                        "type": "update",
                        "timestamp": time.time(),
                        "bots": bots
                    }
                    ws_server.broadcast(payload)
                except Exception as wse:
                     logger.error(f"WS Broadcast Error: {wse}")


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
