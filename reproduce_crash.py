
import sys
import os
import json
import logging
from unittest.mock import MagicMock

# Add root to sys.path
sys.path.append(os.getcwd())

from engine.bot_executor import BotExecutor
import engine.bot_executor
from engine.database import get_connection, get_bot_status

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Reproduce")

# Mock classes
class MockExchange:
    def __init__(self, market_type='future'):
        self.market_type = market_type
        self.exchange = MagicMock()
        # Mock fetch_positions to return empty or specific data
        self.exchange.fetch_positions.return_value = []
        self.exchange.fetch_open_orders.return_value = []
        
    def fetch_ohlcv(self, symbol, timeframe, limit):
        # Return dummy OHLCV data
        import pandas as pd
        import time
        now = int(time.time() * 1000)
        data = []
        for i in range(limit):
             # [time, open, high, low, close, volume]
             data.append([now - (limit-i)*60000, 90000.0, 91000.0, 89000.0, 90000.0, 1.0])
        return data

    def fetch_open_orders(self, symbol):
        return []

    def fetch_positions(self):
        return []
    
    def validate_order(self, *args):
        return True, 0.1, 90000.0, "OK"
        
    def create_order(self, *args, **kwargs):
        return {'id': 'mock_order_123', 'status': 'open'}

    def get_last_price(self, pair):
        return 90000.0

class MockRunner:
    def __init__(self):
        self.strategies = {}
        self.exchange = MockExchange()
        self.orders_this_cycle = 0
        self.orders_today = {}
        self._last_reset_day = ""

# Patch get_thread_exchange to return Mock logic
def mock_get_thread_exchange(market_type='future'):
    return MockExchange(market_type)

engine.bot_executor.get_thread_exchange = mock_get_thread_exchange

def reproduce():
    print("=== REPRODUCING CRASH ===")
    conn = get_connection()
    cursor = conn.cursor()
    
    # Fetch active bots
    cursor.execute("SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    
    runner = MockRunner()
    executor = BotExecutor(runner)
    
    for bot_data in bots:
        name = bot_data[1]
        print(f"Processing {name}...")
        try:
            executor.process_bot(bot_data)
            print(f"  SUCCESS: {name}")
        except Exception as e:
            import traceback
            print(f"  CRASHED: {name} - {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    reproduce()
