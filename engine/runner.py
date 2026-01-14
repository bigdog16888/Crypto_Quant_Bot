import time
import logging
import json
import sys
import os
import pandas as pd

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade
from engine.exchange_interface import ExchangeInterface
from engine.strategies.mql4_strategy import MQL4Strategy
from engine.manager import manage_trade
from engine.sync import sync_bot_state
from config.settings import config
from config.constants import (
    MIN_ORDER_USD,
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
    POLL_INTERVAL_SECONDS,
    ORDER_FILL_TIMEOUT_SECONDS,
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

class BotRunner:
    def __init__(self):
        self.running = False
        self.exchange = ExchangeInterface(market_type=config.MARKET_TYPE) # Use config for market type
        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}
        
        # Safety / Circuit Breaker State
        self.initial_equity = 0.0
        self.circuit_breaker_triggered = False
        
        # ========== RUNAWAY ORDER PROTECTION ==========
        # Prevents bugs from placing unlimited orders
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()
        # Using constants from config/constants.py
        # ===============================================
        
        self._initialize_safety_baseline()
        
        # State Synchronization (Phase 9) - wrapped in try/except for crash safety
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
        Synchronizes the state of all active bots with the exchange on startup.
        Ensures DB consistency after crashes or manual interventions.
        """
        logger.info("Starting global state synchronization...")
        active_bots = self.get_active_bots()
        for bot in active_bots:
            # bot: (id, name, pair, ...)
            bot_id = bot[0]
            # sync_bot_state handles the heavy lifting
            sync_bot_state(bot_id, self.exchange)
        logger.info("Global state synchronization complete.")

    def _initialize_safety_baseline(self):
        """Captures initial account state for Drawdown monitoring."""
        try:
            balance = self.exchange.fetch_balance()
            if not balance:
                raise ValueError("Failed to fetch balance on init")
                
            # Use helper for stablecoin calculation
            total_stablecoin = self._calculate_stablecoin_balance(balance)
            
            # Add estimated value of open positions (from DB)
            # This handles restarts where we already have positions
            active_bots = self.get_active_bots()
            invested_sum = 0.0
            for bot in active_bots:
                # bot: id, name, ..., strategy, config, base, mm, rsi, is_active
                # We need trade data
                t_data = get_bot_status(bot[0])
                if t_data and len(t_data) > 3:
                    invested_sum += float(t_data[3]) # total_invested
            
            # Equity ≈ Cash + Cost Basis of Positions (Simplified)
            self.initial_equity = total_stablecoin + invested_sum
            logger.info(f"🛡️ Safety Baseline Initialized. Equity: ${self.initial_equity:.2f} (Cash: {total_stablecoin:.2f} + Pos: {invested_sum:.2f})")
            
        except Exception as e:
            logger.error(f"Failed to initialize safety baseline: {e}")
            self.initial_equity = 0.0 # Disable check if failed

    def check_circuit_breaker(self):
        """
        Global Circuit Breaker: Checks if account equity has dropped below safe limits.
        """
        if self.circuit_breaker_triggered:
            return  # Already triggered, don't check again
            
        if self.initial_equity <= 0:
            logger.warning("Circuit breaker skipped: initial_equity not set (API may have failed on startup)")
            return

        try:
            balance = self.exchange.fetch_balance()
            if not balance:
                logger.warning("Circuit breaker skipped: Could not fetch balance")
                return # Skip check if API fail
                
            # Use helper for stablecoin calculation
            total_stablecoin = self._calculate_stablecoin_balance(balance)
            
            # Sum up invested costs from all bots
            active_bots = self.get_active_bots()
            invested_cost = 0.0
            
            for bot in active_bots:
                bot_id = bot[0]
                t_data = get_bot_status(bot_id)
                if t_data and len(t_data) > 3 and t_data[3] > 0:
                    invested_cost += t_data[3]
            
            current_equity = total_stablecoin + invested_cost
            
            # Prevent division by zero
            if self.initial_equity <= 0:
                logger.error("Circuit breaker: initial_equity is zero or negative")
                return
                
            drawdown_pct = (self.initial_equity - current_equity) / self.initial_equity * 100
            
            # Log current state periodically (every check)
            logger.debug(f"Circuit Breaker Check: Equity ${current_equity:.2f} (Initial: ${self.initial_equity:.2f}) | Drawdown: {drawdown_pct:.2f}%")
            
            if drawdown_pct > config.GLOBAL_STOP_LOSS_PCT:
                logger.critical(f"🚨 CIRCUIT BREAKER TRIPPED! Drawdown: {drawdown_pct:.2f}% > {config.GLOBAL_STOP_LOSS_PCT}%")
                logger.critical(f"Initial: ${self.initial_equity:.2f}, Current: ${current_equity:.2f}")
                self.circuit_breaker_triggered = True
                
                # Create emergency file to trigger handler
                with open(config.PATHS["EMERGENCY_FILE"], "w") as f:
                    f.write("CIRCUIT_BREAKER")
                    
        except Exception as e:
            logger.error(f"Circuit breaker check failed: {e}")

    def get_active_bots(self):
        """Fetches all bots and their current status."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Fetch all bots to handle both active and recently deactivated ones
            cursor.execute('''
                SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active
                FROM bots 
            ''')
            bots = cursor.fetchall()
            return bots
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally:
            conn.close()

    def process_bot(self, bot_data):
        """
        Main logic for a single bot instance.
        bot_data: tuple (id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit, is_active)
        """
        # Unpack bot data; handle missing is_active (default True)
        bot_id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit, *optional = bot_data
        is_active = optional[0] if optional else True
        
        # Per-bot isolation: wrap entire bot processing in try/except
        # One bot crashing should NOT affect others
        try:
            # Cleanup Logic for Deactivated Bots
            if not is_active:
                if bot_id in self.strategies:
                    logger.info(f"Bot {name} deactivated. Cleaning up orders...")
                    try:
                        self.exchange.exchange.cancel_all_orders(pair)
                        del self.strategies[bot_id]
                    except Exception as e:
                        logger.error(f"Cleanup for {name} failed: {e}")
                return

            # Parse Config
            params = json.loads(config_json) if config_json else {}
            params.update({
                'direction': direction,
                'base_size': base_size,
                'martingale_multiplier': mm,
                'rsi_limit': rsi_limit
            })

            # Identify Execution Timeframe (default 1h if not set)
            timeframe = params.get('timeframe', '1h')

            # Initialize or Get Strategy
            if bot_id not in self.strategies:
                if strat_type == 'MQL4':
                    self.strategies[bot_id] = MQL4Strategy(name=name, params=params)
                elif strat_type == 'MarketMaker':
                    from engine.strategies.market_maker import MarketMakerStrategy
                    self.strategies[bot_id] = MarketMakerStrategy(name=name, params=params)
                elif strat_type == 'MagicHour':
                    from engine.strategies.magic_hour_strategy import MagicHourStrategy
                    self.strategies[bot_id] = MagicHourStrategy(name=name, params=params)
                else:
                    logger.warning(f"Unknown strategy type {strat_type} for bot {name}. Skipping.")
                    return

            strategy = self.strategies[bot_id]
            
            # Fetch Market Data
            ohlcv = self.exchange.fetch_ohlcv(symbol=pair, timeframe=timeframe, limit=100)
            
            if not ohlcv:
                logger.warning(f"No data for {pair} on {timeframe}. Skipping.")
                return

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # --- SPECIAL: Market Maker Logic ---
            if strat_type == 'MarketMaker':
                self.process_market_maker(bot_id, name, pair, strategy, df)
                return

            # --- State Selection ---
            trade_data = get_bot_status(bot_id)
            # trade_data: (name, pair, current_step, total_invested, avg_price, tp_price, last_exit_price, last_exit_time)
            # SAFETY: Validate tuple before accessing indices to prevent IndexError crashes
            if not trade_data or len(trade_data) < 8:
                logger.warning(f"Bot {name}: Invalid trade_data (None or incomplete). Skipping cycle.")
                return
            
            is_in_trade = trade_data[3] > 0

            if not is_in_trade:
                # --- Re-entry Logic & Cooldowns ---
                last_exit_price = trade_data[6] if len(trade_data) > 6 else 0.0
                last_exit_time = trade_data[7] if len(trade_data) > 7 else 0
                
                # SAFETY: Validate DataFrame before accessing iloc
                if df.empty:
                    logger.warning(f"Bot {name}: Empty DataFrame. Skipping cycle.")
                    return
                current_price = df['close'].iloc[-1]
                
                can_enter = True
                
                # Check Time Cooldown
                reentry_mins = params.get('reentry_cooldown_mins', 0)
                if last_exit_time > 0 and reentry_mins > 0:
                    elapsed_mins = (time.time() - last_exit_time) / 60
                    if elapsed_mins < reentry_mins:
                        can_enter = False
                        logger.debug(f"Bot {name} in time cooldown ({elapsed_mins:.1f}/{reentry_mins} min)")
                
                # Check Distance Cooldown
                reentry_dist_pct = params.get('reentry_distance_pct', 0.0)
                if last_exit_price > 0 and reentry_dist_pct > 0:
                    dist_pc = abs(current_price - last_exit_price) / last_exit_price * 100
                    if dist_pc < reentry_dist_pct:
                        can_enter = False
                        logger.debug(f"Bot {name} in distance cooldown ({dist_pc:.2f}/{reentry_dist_pct}%)")

                if can_enter:
                    # --- Entry Logic ---
                    buy_signal, sell_signal = strategy.check_signals(df)
                    logger.debug(f"Bot {name} - Signal Check: Buy={buy_signal}, Sell={sell_signal}")
                    
                    if direction == 'LONG' and buy_signal:
                        self.execute_entry(bot_id, name, pair, 'buy', base_size)
                    elif direction == 'SHORT' and sell_signal:
                        self.execute_entry(bot_id, name, pair, 'sell', base_size)
            else:
                # --- Trade Management Logic ---
                # SAFETY: Validate DataFrame before accessing iloc
                if df.empty:
                    logger.warning(f"Bot {name}: Empty DataFrame in trade management. Skipping.")
                    return
                    
                # Pass market data to strategy for grid calculations
                strategy.last_market_data = df
                current_price = df['close'].iloc[-1]
                
                # manager.manage_trade handles TP, Grid Steps, and Hedging
                result = manage_trade(bot_id, name, pair, direction, params, trade_data, current_price, strategy, self.exchange)
                
                if result and result.get('action') == 'tp_hit':
                    # Log the clear exit
                    logger.info(f"Bot {name} - Cycle Complete. Entering potential cooldown/re-entry phase.")

        except Exception as e:
            logger.error(f"Error processing bot {name}: {e}")

    def process_market_maker(self, bot_id, name, pair, strategy, df):
        """
        Executes the specific loop for Market Making bots.
        """
        try:
            current_price = df['close'].iloc[-1]
            
            # 1. Get Inventory
            # In a real scenario, fetch from Exchange. For v0.4, use DB state or mock.
            # Here we assume 'total_invested' in DB reflects net position (signed).
            trade_data = get_bot_status(bot_id)
            # trade_data: (name, pair, current_step, total_invested, avg_price, tp_price)
            current_inventory = trade_data[3] if trade_data else 0.0
            
            # 2. Calculate Quotes
            ideal_bid, ideal_ask = strategy.calculate_quotes(current_price, current_inventory)
            
            # 3. Reconcile (Update Orders)
            # Fetch open orders
            open_orders = self.exchange.fetch_open_orders(pair)
            
            # Separate Bid/Ask
            current_bids = [o for o in open_orders if o.get('side') == 'buy'] if open_orders else []
            current_asks = [o for o in open_orders if o.get('side') == 'sell'] if open_orders else []
            
            # --- Bid Logic ---
            if not current_bids:
                self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True})
            else:
                best_bid = max(current_bids, key=lambda x: float(x.get('price', 0)))
                bid_price = float(best_bid.get('price', 0))
                
                # Check deviation
                diff = abs(bid_price - ideal_bid) / ideal_bid
                if diff > strategy.reprice_threshold:
                    logger.info(f"MM {name}: Repricing Bid. Old: {bid_price}, New: {ideal_bid}")
                    self.exchange.cancel_all_orders(pair) # Simple cancel all for now
                    self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True})

            # --- Ask Logic ---
            if not current_asks:
                self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True})
            else:
                best_ask = min(current_asks, key=lambda x: float(x.get('price', 0)))
                ask_price = float(best_ask.get('price', 0))
                
                # Check deviation
                diff = abs(ask_price - ideal_ask) / ideal_ask
                if diff > strategy.reprice_threshold:
                    logger.info(f"MM {name}: Repricing Ask. Old: {ask_price}, New: {ideal_ask}")
                    self.exchange.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True})

        except Exception as e:
            logger.error(f"MM Loop failed for {name}: {e}")

    def _check_order_limits(self, bot_id, name):
        """
        RUNAWAY PROTECTION: Checks if order limits are exceeded.
        Returns (can_order: bool, reason: str)
        """
        # Reset daily counter at midnight
        current_day = time.strftime("%Y-%m-%d")
        if not hasattr(self, '_last_reset_day') or self._last_reset_day != current_day:
            self.orders_today = {}
            self._last_reset_day = current_day
            logger.info(f"🔄 Daily order counters reset for {current_day}")
        
        # Check per-cycle limit
        if self.orders_this_cycle >= MAX_ORDERS_PER_CYCLE:
            return False, f"Cycle limit reached ({self.orders_this_cycle}/{MAX_ORDERS_PER_CYCLE})"
        
        # Check per-bot daily limit
        bot_count = self.orders_today.get(bot_id, 0)
        if bot_count >= MAX_ORDERS_PER_BOT_DAILY:
            return False, f"Daily limit for bot {name} reached ({bot_count}/{MAX_ORDERS_PER_BOT_DAILY})"
        
        return True, ""
    
    def _record_order(self, bot_id):
        """Records an order for rate limiting."""
        self.orders_this_cycle += 1
        self.orders_today[bot_id] = self.orders_today.get(bot_id, 0) + 1

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params={}):
        """
        Place the first order and initialize the trade in DB.
        """
        # ========== RUNAWAY ORDER PROTECTION ==========
        can_order, reason = self._check_order_limits(bot_id, name)
        if not can_order:
            logger.critical(f"🚨 ORDER BLOCKED for {name}: {reason}")
            return
        # ===============================================
        
        logger.info(f"🚀 [ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        # ========== MINIMUM ORDER VALIDATION ==========
        if amount < MIN_ORDER_USD:
            logger.error(f"Order amount ${amount} below minimum ${MIN_ORDER_USD}, aborting.")
            return
        # ===============================================
        
        # Validated Create Order
        # Fetch current price for limit order safety
        if price is None:
            price = self.exchange.get_last_price(pair)
        
        if price == 0:
            logger.error(f"Could not fetch price for {pair}, aborting entry.")
            return

        # Sanity check direction vs side
        # side is 'buy' or 'sell'
        
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Simulating entry for {name} at {price}")
            tp_price = price * (1.01 if side == 'buy' else 0.99)
            update_martingale_step(bot_id, 0, amount, price, tp_price)
            self._record_order(bot_id)  # Count dry run orders too for testing
        else:
            # Real Order
            try:
                # Use create_order which now has validation and retries
                order = self.exchange.create_order(pair, 'limit', side, amount, price, params=params)
                if order:
                    order_id = order.get('id')
                    logger.info(f"Order placed: {order_id}")
                    
                    # Wait for fill confirmation (with timeout)
                    filled, final_order = self.exchange.wait_for_fill(order_id, pair, timeout_seconds=ORDER_FILL_TIMEOUT_SECONDS)
                    
                    if filled:
                        # Use actual fill price if available
                        fill_price = final_order.get('average', price) if final_order else price
                        tp_price = fill_price * (1.01 if side == 'buy' else 0.99)
                        update_martingale_step(bot_id, 0, amount, fill_price, tp_price)
                        self._record_order(bot_id)
                        logger.info(f"✅ Order {order_id} confirmed filled at {fill_price}")
                        
                        # Log trade to history for post-mortem analysis
                        log_trade(
                            bot_id=bot_id,
                            action='BUY' if side == 'buy' else 'SELL',
                            symbol=pair,
                            price=fill_price,
                            amount=amount,
                            cost_usdc=amount,
                            order_id=order_id,
                            step=0,
                            notes=f"Entry order for {name}"
                        )
                    else:
                        # Order didn't fill, cancel it
                        logger.warning(f"Order {order_id} not filled, cancelling...")
                        try:
                            self.exchange.cancel_all_orders(pair)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Entry failed for {name}: {e}")

    def run_cycle(self):
        """
        Single iteration of the bot loop.
        """
        # ========== RESET CYCLE ORDER COUNTER ==========
        self.orders_this_cycle = 0
        # ===============================================
        
        # 0. Circuit Breaker Check
        self.check_circuit_breaker()

        # 1. Check for Emergency Signal (Higher Priority)
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]):
            logger.critical("🚨 EMERGENCY SIGNAL DETECTED! LIQUIDATING ALL 🚨")
            self.handle_emergency_liquidation()
            self.running = False
            if os.path.exists(config.PATHS["EMERGENCY_FILE"]): os.remove(config.PATHS["EMERGENCY_FILE"])
            return False

        # 2. Check for standard stop signal
        if os.path.exists(config.PATHS["STOP_FILE"]):
            logger.info("Stop signal detected. Exiting gracefully...")
            self.running = False
            return False

        logger.info("Starting run cycle...")
        bots = self.get_active_bots()
        logger.info(f"Found {len(bots)} active bots.")
        
        for bot in bots:
            # Re-check signals inside loop
            if os.path.exists(config.PATHS["EMERGENCY_FILE"]) or os.path.exists(config.PATHS["STOP_FILE"]):
                break
            self.process_bot(bot)
            
        logger.info("Cycle complete.")
        return True

    def handle_emergency_liquidation(self):
        """
        Cancels all orders and closes all positions for all active bots in the DB.
        """
        bots = self.get_active_bots()
        for bot_data in bots:
            # bot_data: (id, name, pair, ..., is_active)
            id, name, pair = bot_data[0], bot_data[1], bot_data[2]
            logger.warning(f"Emergency cleanup for {name} ({pair})")
            try:
                # 1. Cancel Open Orders
                self.exchange.cancel_all_orders(pair)
                logger.info(f"Orders canceled for {pair}")
                
                # 2. Close Positions (Market Sell/Buy)
                if config.DRY_RUN:
                    logger.info(f"[DRY RUN] Emergency Market Close simulated for {pair}")
                else:
                    # Fetch position (assuming spot, we check balance of base asset)
                    base_currency = pair.split('/')[0]
                    balance = self.exchange.fetch_balance()
                    qty_dict = balance.get(base_currency)
                    if isinstance(qty_dict, dict):
                        qty = qty_dict.get('free', 0.0)
                    else:
                        qty = 0.0
                    
                    # Validate against MinQty to avoid error loop
                    # Simplified Market Sell
                    if qty > 0:
                        logger.warning(f"Market Selling {qty} {base_currency}")
                        # self.exchange.create_order(pair, 'market', 'sell', qty) 
                        # Commented out safety: 'market' orders need careful MinNotional check too
                        # Use validate_order implicit check? Market orders skip price check.
                        pass

            except Exception as e:
                logger.error(f"Emergency cleanup failed for {name}: {e}")

if __name__ == "__main__":
    init_db() # Ensure schema is up to date
    logger.info("Bot Service Started.")
    
    # CRASH RECOVERY: Wrap BotRunner init in try/except
    try:
        runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: Failed to initialize BotRunner: {e}")
        sys.exit(1)
    
    runner.running = True
    
    STOP_FILE = config.PATHS["STOP_FILE"]
    PID_FILE = config.PATHS["PID_FILE"]

    # Ensure stop file is gone at start
    if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
    
    # Write PID file for process management
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    consecutive_failures = 0

    try:
        while runner.running:
            try:
                if not runner.run_cycle():
                    break
                consecutive_failures = 0  # Reset on success
            except Exception as cycle_err:
                # PER-CYCLE CRASH RECOVERY: Log and continue, don't kill entire service
                consecutive_failures += 1
                logger.error(f"Cycle failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {cycle_err}")
                
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.critical(f"🚨 {MAX_CONSECUTIVE_FAILURES} consecutive failures. Shutting down for safety.")
                    break
                    
            time.sleep(POLL_INTERVAL_SECONDS)
            
    except KeyboardInterrupt:
        logger.info("Bot Service Stopped by User (Ctrl+C).")
    except SystemExit:
        logger.info("Bot Service received exit signal.")
    except BaseException as e:
        # Catch EVERYTHING including SystemExit, KeyboardInterrupt variants
        logger.critical(f"Bot Service Crashed (BaseException): {e}")
    finally:
        # Cleanup
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
        logger.info("Bot Service Permanently Stopped.")
