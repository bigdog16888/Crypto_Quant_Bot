import sys
import os
import sqlite3
import pandas as pd

# Add root to sys.path
sys.path.append(os.getcwd())

from engine.strategies.mql4_strategy import MQL4Strategy
from engine.strategies.market_maker import MarketMakerStrategy
from engine.exchange_interface import ExchangeInterface
from engine.database import init_db, get_connection

def verify_strategies():
    print("\n[1] Verifying Strategy Modules...")
    
    # 1. MQL4 Strategy
    try:
        mql4 = MQL4Strategy(name="Legacy_Test")
        # Test signal check with dummy data
        df = pd.DataFrame({
            'open': [100, 101, 102],
            'high': [105, 106, 107],
            'low': [95, 96, 97],
            'close': [102, 103, 104],
            'volume': [1000, 1500, 1200]
        })
        buy, sell = mql4.check_signals(df)
        print(f"  - MQL4Strategy instantiated. Signals: Buy={buy}, Sell={sell}")
    except Exception as e:
        print(f"  ! MQL4Strategy Failed: {e}")

    # 2. Market Maker Strategy
    try:
        mm = MarketMakerStrategy(name="MM_Test")
        buy, sell = mm.check_signals(df) 
        print(f"  - MarketMakerStrategy instantiated. Signals: Buy={buy}, Sell={sell}")
    except Exception as e:
        print(f"  ! MarketMakerStrategy Failed: {e}")

def verify_exchange():
    print("\n[2] Verifying Exchange Interface (Futures/USDC)...")
    try:
        # Initialize as Swap (Futures)
        exchange = ExchangeInterface(market_type='swap')
        print(f"  - Exchange Interface initialized for Swap.")
        
        # Test dry run fetching
        # Note: In dry run, actual network calls might be skipped unless allowed. 
        # I enabled them for fetch* in my edit.
        symbols = exchange.get_available_symbols(quote_asset='USDC')
        print(f"  - Fetched USDC symbols (Count: {len(symbols)})")
        if len(symbols) > 0:
            print(f"  - Sample: {symbols[:3]}")
        else:
            print("  - No symbols found (Check API keys or network if not in Dry Run, or if Dry Run mocks empty)")
            
    except Exception as e:
        print(f"  ! Exchange Verification Failed: {e}")

def verify_database():
    print("\n[3] Verifying Database Schema...")
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check for strategy_type column
    try:
        cursor.execute("SELECT strategy_type, config FROM bots LIMIT 0")
        print("  - Columns 'strategy_type' and 'config' exist in 'bots' table.")
    except sqlite3.OperationalError as e:
        print(f"  ! Column check Failed: {e}")
        
    # Test inserting a bot with config
    try:
        import json
        test_config = {"UseATRGrid": True, "TestParam": 123}
        cursor.execute("INSERT INTO bots (name, pair, direction, strategy_type, config) VALUES (?, ?, ?, ?, ?)", 
                      ("ConfigTestBot", "BTC/USDT", "BUY", "TEST", json.dumps(test_config)))
        conn.commit()
        print("  - Successfully inserted bot with JSON config.")
        
        # Read back
        cursor.execute("SELECT config FROM bots WHERE name='ConfigTestBot'")
        row = cursor.fetchone()
        loaded_config = json.loads(row[0])
        if loaded_config.get("TestParam") == 123:
             print("  - Successfully verified JSON config read-back.")
        else:
             print(f"  ! Config read-back mismatch: {loaded_config}")
             
        # Cleanup
        cursor.execute("DELETE FROM bots WHERE name='ConfigTestBot'")
        conn.commit()
        
    except Exception as e:
        # Ignore unique constraint if run multiple times, or print error
        if "UNIQUE constraint" in str(e):
             print("  - ConfigTestBot already exists (Skipping insert test).")
        else:
             print(f"  ! Config Insert Verification Failed: {e}")
        
    conn.close()

if __name__ == "__main__":
    print("--- Architecture Verification ---")
    verify_database()
    verify_strategies()
    verify_exchange()
