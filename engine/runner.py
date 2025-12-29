import time
import logging
import json
import sys
import os
import pandas as pd

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface
from engine.strategies.mql4_strategy import MQL4Strategy
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
        """Fetches all active bots from the database."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Fetch bots where is_active = 1
            cursor.execute('''
                SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit 
                FROM bots 
                WHERE is_active = 1
            ''')
            bots = cursor.fetchall()
            return bots
        except Exception as e:
            logger.error(f"Error fetching active bots: {e}")
            return []
        finally:
            conn.close()

    def process_bot(self, bot_data):
        """
        Main logic for a single bot instance.
        bot_data: tuple (id, name, pair, direction, strategy_type, config_json, base_size, mm, rsi_limit)
        """
        bot_id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit = bot_data
        
        try:
            # Parse Config
            params = json.loads(config_json) if config_json else {}
            # Merge schema params into params dict for strategy ease of use
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
            # We need enough data for the longest indicator. 100 candles usually safe for standard indicators.
            ohlcv = self.exchange.fetch_ohlcv(symbol=pair, timeframe=timeframe, limit=100)
            
            if not ohlcv:
                logger.warning(f"No data for {pair} on {timeframe}. Skipping.")
                return

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms') # Set formatted timestamp if needed, but keeping separate col is fine

            # Check Signals
            buy_signal, sell_signal = strategy.check_signals(df)
            
            logger.info(f"Bot {name} ({pair} {timeframe}) - Buy: {buy_signal}, Sell: {sell_signal}")

            # Execution Logic (Dry Run Wrapper)
            if direction == 'LONG' and buy_signal:
                self.execute_entry(bot_id, name, pair, 'buy', base_size)
            elif direction == 'SHORT' and sell_signal:
                self.execute_entry(bot_id, name, pair, 'sell', base_size)

        except Exception as e:
            logger.error(f"Error processing bot {name}: {e}")

    def execute_entry(self, bot_id, name, pair, side, amount):
        """
        Place an order.
        """
        logger.info(f"!!! SIGNAL DETECTED !!! Bot: {name} | Pair: {pair} | Side: {side} | Amount: {amount}")
        
        # Real execution would go here:
        # response = self.exchange.create_order(pair, 'market', side, amount)
        
        # For Smart Start / Verification, we just log heavily.
        if config.DRY_RUN:
            logger.info("[DRY RUN] Order simulation successful.")
        else:
            # TODO: Call exchange.create_order and update DB trades table
            pass

    def run_cycle(self):
        """
        Single iteration of the bot loop.
        """
        logger.info("Starting run cycle...")
        bots = self.get_active_bots()
        logger.info(f"Found {len(bots)} active bots.")
        
        for bot in bots:
            self.process_bot(bot)
            
        logger.info("Cycle complete.")

if __name__ == "__main__":
    logger.info("Bot Service Started.")
    runner = BotRunner()
    
    try:
        while True:
            runner.run_cycle()
            time.sleep(10) # 10 second polling
    except KeyboardInterrupt:
        logger.info("Bot Service Stopped by User.")
    except Exception as e:
        logger.critical(f"Bot Service Crashed: {e}")

