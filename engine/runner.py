import time
import logging
import json
import sys
import os
import pandas as pd

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step
from engine.exchange_interface import ExchangeInterface
from engine.strategies.mql4_strategy import MQL4Strategy
from engine.manager import manage_trade
from config.settings import config

# Configure logging
# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("engine.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BotRunner")

class BotRunner:
    def __init__(self):
        self.running = False
        self.exchange = ExchangeInterface(market_type='spot') # Defaulting to spot for now, can be dynamic per bot
        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}

    def get_active_bots(self):
        """Fetches all bots and their current status."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Fetch all bots to handle sowohl active als auch recently deactivated ones
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

            # --- State Selection ---
            trade_data = get_bot_status(bot_id)
            # trade_data: (name, pair, current_step, total_invested, avg_price, tp_price)
            is_in_trade = trade_data[3] > 0 if trade_data else False

            if not is_in_trade:
                # --- Re-entry Logic & Cooldowns ---
                last_exit_price = trade_data[6]
                last_exit_time = trade_data[7]
                current_price = df['close'].iloc[-1]
                
                can_enter = True
                
                # Check Time Cooldown
                reentry_mins = params.get('reentry_cooldown_mins', 0)
                if last_exit_time > 0 and reentry_mins > 0:
                    import time
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
                # Pass market data to strategy for grid calculations
                strategy.last_market_data = df
                current_price = df['close'].iloc[-1]
                
                # manager.manage_trade handles TP, Grid Steps, and Hedging
                result = manage_trade(bot_id, name, pair, direction, params, trade_data, current_price, strategy, self.exchange)
                
                if result.get('action') == 'tp_hit':
                    # Log the clear exit
                    logger.info(f"Bot {name} - Cycle Complete. Entering potential cooldown/re-entry phase.")

        except Exception as e:
            logger.error(f"Error processing bot {name}: {e}")

    def execute_entry(self, bot_id, name, pair, side, amount):
        """
        Place the first order and initialize the trade in DB.
        """
        logger.info(f"🚀 [ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Simulating entry for {name}")
            # Initialize trade in DB (Step 0, total = amount, avg = current_price, tp = avg + 1%)
            # We need current price
            ohlcv = self.exchange.fetch_ohlcv(pair, timeframe='1m', limit=1)
            current_price = ohlcv[0][4] if ohlcv else 0.0
            
            tp_price = current_price * (1.01 if side == 'buy' else 0.99)
            update_martingale_step(bot_id, 0, amount, current_price, tp_price)
        else:
            # TODO: Call exchange.create_order
            pass

    def run_cycle(self):
        """
        Single iteration of the bot loop.
        """
        # 1. Check for Emergency Signal (Higher Priority)
        if os.path.exists("engine.emergency"):
            logger.critical("🚨 EMERGENCY SIGNAL DETECTED! LIQUIDATING ALL 🚨")
            self.handle_emergency_liquidation()
            self.running = False
            if os.path.exists("engine.emergency"): os.remove("engine.emergency")
            return False

        # 2. Check for standard stop signal
        if os.path.exists("engine.stop"):
            logger.info("Stop signal detected. Exiting gracefully...")
            self.running = False
            return False

        logger.info("Starting run cycle...")
        bots = self.get_active_bots()
        logger.info(f"Found {len(bots)} active bots.")
        
        for bot in bots:
            # Re-check signals inside loop
            if os.path.exists("engine.emergency") or os.path.exists("engine.stop"):
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
                self.exchange.exchange.cancel_all_orders(pair)
                # In a real scenario, we'd fetch position and MARKET CLOSE it here.
                # For dry run/smart start, we just log it.
                logger.info(f"Emergency: All orders canceled for {pair}")
                if config.DRY_RUN:
                    logger.info(f"[DRY RUN] Emergency Market Close simulated for {pair}")
                else:
                    # TODO: Implement actual position fetching and market closing
                    pass
            except Exception as e:
                logger.error(f"Emergency cleanup failed for {name}: {e}")

if __name__ == "__main__":
    init_db() # Ensure schema is up to date
    logger.info("Bot Service Started.")
    runner = BotRunner()
    runner.running = True
    
    STOP_FILE = "engine.stop"
    PID_FILE = "engine.pid"

    # Ensure stop file is gone at start
    if os.path.exists(STOP_FILE): os.remove(STOP_FILE)

    try:
        while runner.running:
            if not runner.run_cycle():
                break
            time.sleep(10) # 10 second polling
    except KeyboardInterrupt:
        logger.info("Bot Service Stopped by User (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Bot Service Crashed: {e}")
    finally:
        # Cleanup
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
        logger.info("Bot Service Permanently Stopped.")

