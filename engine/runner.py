import time
import logging
import json
import sys
import os
import pandas as pd
import ccxt
from concurrent.futures import ThreadPoolExecutor
import threading

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.bot_executor import BotExecutor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME
from engine.reconciliation import sync_all_bots
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

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.PATHS["LOG_FILE"]),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BotRunner")

# Thread-local storage for ExchangeInterface instances
# This ensures each thread in the ThreadPoolExecutor gets its own exchange connection
# preventing race conditions on CCXT's internal state (nonce, request signing, etc.)
thread_local_storage = threading.local()

def get_thread_exchange(market_type='future'):
    """
    Get or create a thread-local ExchangeInterface instance.
    Prevents CCXT concurrency issues.
    """
    if not hasattr(thread_local_storage, "exchanges"):
        thread_local_storage.exchanges = {}
    
    if market_type not in thread_local_storage.exchanges:
        # Create new instance for this thread
        # Note: This triggers fetch_markets/inject_markets on first use per thread
        thread_local_storage.exchanges[market_type] = ExchangeInterface(market_type=market_type)
        
    return thread_local_storage.exchanges[market_type]

class BotRunner:
    def __init__(self):
        self.running = False
        # Main thread exchanges (kept for global ops like check_circuit_breaker)
        # Skip spot exchange if in FUTURES_ONLY_MODE (e.g., testnet with futures-only keys)
        if getattr(config, 'FUTURES_ONLY_MODE', False):
            self.exchanges = {
                'future': ExchangeInterface(market_type='future')
            }
        else:
            self.exchanges = {
                'spot': ExchangeInterface(market_type='spot'),
                'future': ExchangeInterface(market_type='future')
            }
        # For backward compatibility and global actions
        self.exchange = self.exchanges.get(config.MARKET_TYPE, self.exchanges['future'])
        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}
        
        # Safety / Circuit Breaker State
        self.initial_equity = 0.0
        self.circuit_breaker_triggered = False
        
        # ========== RUNAWAY ORDER PROTECTION ==========
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()
        
        self._initialize_safety_baseline()
        
        # State Synchronization
        try:
            self.sync_all_bots()
        except Exception as e:
            logger.error(f"Failed to sync bots on startup (non-fatal): {e}")

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
            try:
                # Use exchange_interface wrapper method if available, or direct ccxt
                # fetch_positions() returns all open positions
                all_positions = self.exchange.exchange.fetch_positions()
                for p in all_positions:
                    # Binance returns contracts or size
                    size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if size != 0:
                        has_position_map[p['symbol']] = True
            except Exception as e:
                logger.error(f"Reconciliation halted: Failed to fetch positions: {e}")
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


    def _calculate_unrealized_pnl(self) -> float:
        """Calculates total unrealized PnL across all active market types."""
        total_unrealized_pnl = 0.0
        active_market_types = set()
        
        # Get market types from currently available exchanges
        for mt in self.exchanges.keys():
             active_market_types.add(mt)
             
        for mt in active_market_types:
            try:
                ex = self.exchanges[mt]
                all_positions = ex.fetch_positions()
                for p in all_positions:
                    pnl = float(p.get('unrealizedPnl', 0.0) or 0.0)
                    total_unrealized_pnl += pnl
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

    def check_circuit_breaker(self):
        """
        Global Circuit Breaker: Checks if account equity has dropped below safe limits.
        """
        # Skip circuit breaker entirely if NO_API_MODE (no balance to check)
        if getattr(config, 'NO_API_MODE', False):
            return
            
        if self.circuit_breaker_triggered or self.initial_equity <= 0:
            return

        try:
            # Only check active market types
            active_bots_raw = self.get_active_bots()
            active_bots = [b for b in active_bots_raw if b[9] == 1]
            active_market_types = set()
            for bot in active_bots:
                config_json = bot[5]
                config_dict = json.loads(config_json) if config_json else {}
                active_market_types.add(config_dict.get('market_type', config.MARKET_TYPE))
            
            if not active_market_types:
                active_market_types.add(config.MARKET_TYPE)

            total_stablecoin = 0.0
            balance_fetch_success = False
            for mt in active_market_types:
                if mt in self.exchanges:
                    try:
                        balance = self.exchanges[mt].fetch_balance()
                        if balance:
                            total_stablecoin += self._calculate_stablecoin_balance(balance)
                            balance_fetch_success = True
                    except Exception: pass
            
            # BUG FIX: If balance fetch failed (auth error), don't trigger circuit breaker
            # Just log and skip this check cycle
            if not balance_fetch_success:
                logger.warning("Circuit breaker check skipped - balance fetch failed (auth/API error)")
                return
                
            invested_cost = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and len(t_data) > 3 and t_data[3] > 0:
                    invested_cost += float(t_data[3])
            
            unrealized_pnl = self._calculate_unrealized_pnl()
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

        self.orders_this_cycle = 0
        self.check_circuit_breaker()
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]):
            self.handle_emergency_liquidation()
            self.running = False
            return False
        if os.path.exists(config.PATHS["STOP_FILE"]):
            self.running = False
            return False

        bots = self.get_active_bots()
        
        # Initialize BotExecutor once per cycle, passing self for state
        bot_executor = BotExecutor(self)

        # Parallel Execution: Process bots in concurrent threads
        # This dramatically reduces loop time from (N * latency) to (latency)
        # Using 5 workers to be safe with rate limits initially
        with ThreadPoolExecutor(max_workers=5) as executor:
            # list() forces execution and waits for completion
            list(executor.map(bot_executor.process_bot, bots))
        
        # Ownership reconciliation: Check for owner failover and stale ownerships
        # logger.debug("Starting ownership reconciliation...")
        self._reconcile_ownership()
        # logger.debug("Ownership reconciliation complete.")
        
        # Publish cycle time
        end_time = time.time()
        BOT_CYCLE_TIME.set(end_time - start_time)

        return True

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
    while runner.running:
        try:
            if not runner.run_cycle(): break
            failures = 0
            
            # Heartbeat every 60s to confirm system is alive
            if time.time() - last_heartbeat > 60:
                logger.info("💓 System Heartbeat - Active")
                last_heartbeat = time.time()
                
        except Exception as e:
            failures += 1
            logger.error(f"Cycle failed ({failures}): {e}")
            if failures >= MAX_CONSECUTIVE_FAILURES: break
        time.sleep(POLL_INTERVAL_SECONDS)
    # === METRICS SERVER STOP ===
    metrics_server.stop()
    if os.path.exists(PID): os.remove(PID)
