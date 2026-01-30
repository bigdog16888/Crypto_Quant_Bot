
import sys
import os
import sqlite3
import logging
import time

# Add root to sys.path
sys.path.append(os.getcwd())

from engine.bot_executor import BotExecutor
from engine.database import get_connection, save_bot_order, get_bot_order_ids, update_order_status
from config.settings import config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Verification")

# Mock Exchange
class MockExchange:
    def __init__(self):
        self.orders = {} # {id: order_dict}
        self.exchange = self
    
    def fetch_open_orders(self, pair):
        return list(self.orders.values())
        
    def cancel_order(self, order_id, pair):
        if order_id in self.orders:
            print(f"[MOCK] Cancelled order {order_id}")
            del self.orders[order_id]
        else:
            print(f"[MOCK] Failed to cancel {order_id} (Not found)")

def setup_db():
    conn = get_connection()
    c = conn.cursor()
    # Create a dummy bot
    c.execute("""
        INSERT OR REPLACE INTO bots 
        (id, name, pair, direction, strategy_type, rsi_limit, martingale_multiplier, base_size, is_active) 
        VALUES (999, 'TestBot', 'BTC/USDT', 'LONG', 'Martingale', 70, 1.5, 10.0, 1)
    """)
    conn.commit()
    return 999

def verify():
    bot_id = setup_db()
    
    # 1. Setup Logic
    # Case A: Bot is Scanning (Not in Trade)
    # But it has a 'grid' order in DB (Ghost/Stale)
    # And that order exists on Exchange
    
    mock_ex = MockExchange()
    # Manual Order (Should be ignored)
    mock_ex.orders['manual_1'] = {'id': 'manual_1', 'symbol': 'BTC/USDT', 'type': 'limit', 'side': 'buy', 'price': 50000}
    # Stale Bot Order (Should be cancelled)
    mock_ex.orders['stale_grid'] = {'id': 'stale_grid', 'symbol': 'BTC/USDT', 'type': 'limit', 'side': 'buy', 'price': 49000}
    
    # Register stale grid in DB
    save_bot_order(bot_id, 'grid', 'stale_grid', 49000, 0.1, 1)
    
    print("=== STARTING VERIFICATION ===")
    print(f"Initial Exchange Orders: {list(mock_ex.orders.keys())}")
    
    # Run Reconciliation
    executor = BotExecutor(None) # Runner not needed for this method
    
    # Simulate Scanning State (is_in_trade=False)
    snapshot = mock_ex.fetch_open_orders('BTC/USDT')
    executor.reconcile_orders(bot_id, 'TestBot', 'BTC/USDT', False, snapshot, mock_ex)
    
    # Check Results
    remaining = list(mock_ex.orders.keys())
    print(f"Final Exchange Orders: {remaining}")
    
    if 'stale_grid' not in remaining:
        print("✅ SUCCESS: Stale grid order was cancelled.")
    else:
        print("❌ FAILURE: Stale grid order remains.")
        
    if 'manual_1' in remaining:
        print("✅ SUCCESS: Manual order was preserved.")
    else:
        print("❌ FAILURE: Manual order was incorrectly cancelled.")

    # Clean up DB
    conn = get_connection()
    conn.execute("DELETE FROM bot_orders WHERE bot_id=999")
    conn.execute("DELETE FROM bots WHERE id=999")
    conn.commit()

if __name__ == "__main__":
    verify()
