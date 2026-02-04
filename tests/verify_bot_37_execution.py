import sys
import os
import sqlite3
import json
import pandas as pd
import logging

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.strategies.martingale_strategy import MartingaleStrategy

# Setup Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VerifyBot37")

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def get_bot_config(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT name, config FROM bots WHERE id = ?', (bot_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        logger.error(f"Bot {bot_id} not found in DB!")
        return None, None
        
    return row[0], json.loads(row[1])

def verify_trigger_logic(bot_id, test_price):
    name, params = get_bot_config(bot_id)
    if not params: return
    
    logger.info(f"--- Verifying Bot {bot_id}: {name} ---")
    
    # 1. Inspect Relevant Params
    mode_price = params.get('mode_price', 0)
    thresh = params.get('price_threshold', 0.0)
    logger.info(f"Config: mode_price={mode_price}, threshold={thresh}")
    logger.info(f"Test Price: {test_price}")
    
    if mode_price == 0:
        logger.warning("Price Trigger is DISABLED (Mode 0). Should not trigger based on price.")
    
    # 2. Setup Strategy
    strategy = MartingaleStrategy(name=name, params=params)
    
    # 3. Create Mock Data (Flat data at test_price)
    # We need enough data for indicators (e.g. 200 candles)
    # If other indicators are active (Check Active Triggers), they might block it.
    # We saw in check_active_triggers that ONLY mode_price: 1 is active.
    
    data = pd.DataFrame({
        'open': [test_price] * 200,
        'high': [test_price] * 200,
        'low': [test_price] * 200,
        'close': [test_price] * 200,
        'volume': [1000.0] * 200,
        'timestamp': pd.date_range(end=pd.Timestamp.now(), periods=200, freq='1h')
    }).set_index('timestamp')
    
    # 4. Run Check
    buy, sell = strategy.check_signals(data)
    
    logger.info(f"Result -> Buy: {buy}, Sell: {sell}")
    
    # 5. Validation
    if mode_price == 1: # Above
        if test_price > thresh:
            if buy: logger.info("✅ SUCCESS: Triggered as expected (Price > Thresh).")
            else: logger.error("❌ FAILURE: Did NOT trigger even though Price > Thresh.")
        else:
            if not buy: logger.info("✅ SUCCESS: Correctly blocked (Price <= Thresh).")
            else: logger.error("❌ FAILURE: Triggered incorrectly (Price <= Thresh).")
            
    elif mode_price == 2: # Below
        if test_price < thresh:
            if buy: logger.info("✅ SUCCESS: Triggered as expected (Price < Thresh).")
            else: logger.error("❌ FAILURE: Did NOT trigger even though Price < Thresh.")
        else:
            if not buy: logger.info("✅ SUCCESS: Correctly blocked (Price >= Thresh).")
            else: logger.error("❌ FAILURE: Triggered incorrectly (Price >= Thresh).")

if __name__ == "__main__":
    # Threshold is 68350.0
    # Test Case 1: Price 70,000 (Should Trigger)
    verify_trigger_logic(37, 70000.0)
    
    print("\n" + "="*30 + "\n")
    
    # Test Case 2: Price 60,000 (Should FAIL)
    verify_trigger_logic(37, 60000.0)
