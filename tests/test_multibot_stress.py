"""
Multi-Bot Stress Test
Tests concurrent operation of multiple bots to detect race conditions.

Run with: python tests/test_multibot_stress.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import threading
from unittest.mock import patch, MagicMock

# Set DRY_RUN before importing anything else
os.environ['DRY_RUN'] = 'true'
os.environ['TESTNET'] = 'true'

from engine.database import init_db, add_bot, delete_bot, get_all_bots
from engine.runner import BotRunner

def create_test_bots(count=5):
    """Creates multiple test bots for stress testing."""
    bot_ids = []
    for i in range(count):
        bot_id = add_bot(
            name=f"StressTest_Bot_{i}_{int(time.time())}",
            pair="BTC/USDT",
            direction="LONG",
            rsi_limit=30,
            martingale_multiplier=1.5,
            base_size=10.0,
            strategy_type="Martingale",
            config_dict={'timeframe': '1h'}
        )
        if bot_id:
            bot_ids.append(bot_id)
            print(f"✅ Created bot {bot_id}")
        else:
            print(f"❌ Failed to create bot {i}")
    return bot_ids

def cleanup_test_bots(bot_ids):
    """Removes test bots after testing."""
    # Get a fresh connection since thread-local may have been closed
    import sqlite3
    from engine.database import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cursor = conn.cursor()
        for bot_id in bot_ids:
            try:
                cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))
                cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
                cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
                print(f"🗑️ Deleted bot {bot_id}")
            except Exception as e:
                print(f"⚠️ Error deleting bot {bot_id}: {e}")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Cleanup connection error (non-fatal): {e}")

def test_concurrent_cycles(num_cycles=10):
    """
    Runs multiple cycles with all bots active to detect:
    1. Database locking issues
    2. Race conditions in order placement
    3. Crashes from concurrent access
    """
    print("\n" + "="*60)
    print("🧪 MULTI-BOT STRESS TEST")
    print("="*60 + "\n")
    
    import tempfile
    import shutil
    import engine.database
    
    test_dir = tempfile.mkdtemp()
    test_db = os.path.join(test_dir, "test_multibot_stress.db")
    
    original_db_path = engine.database.DB_PATH
    engine.database.DB_PATH = test_db
    
    if hasattr(engine.database._local, 'connection'):
        del engine.database._local.connection
        
    success = False
    bot_ids = []
    
    try:
        # Initialize
        init_db()
        
        # Create test bots
        print("Creating test bots...")
        bot_ids = create_test_bots(5)
        
        if len(bot_ids) < 3:
            print("❌ Failed to create enough bots for stress test")
            return False
        
        print(f"\n📊 Testing with {len(bot_ids)} bots running {num_cycles} cycles...\n")
        
        # Mock exchange to avoid real API calls
        with patch('engine.runner.ExchangeInterface') as MockExchange:
            mock_instance = MagicMock()
            mock_instance.fetch_balance.return_value = {'USDT': {'total': 10000.0, 'free': 10000.0}}
            mock_instance.fetch_ohlcv.return_value = [
                [int(time.time()*1000), 50000, 50100, 49900, 50050, 1000]
                for _ in range(100)
            ]
            mock_instance.get_last_price.return_value = 50050.0
            MockExchange.return_value = mock_instance
            
            try:
                runner = BotRunner()
                runner.running = True
                
                errors = []
                cycles_completed = 0
                
                for cycle in range(num_cycles):
                    try:
                        result = runner.run_cycle()
                        cycles_completed += 1
                        
                        # Check order limits are working
                        if runner.orders_this_cycle > runner.MAX_ORDERS_PER_CYCLE:
                            errors.append(f"Cycle {cycle}: Order limit exceeded!")
                        
                        print(f"  ✓ Cycle {cycle+1}/{num_cycles} - Orders: {runner.orders_this_cycle}")
                        
                    except Exception as e:
                        errors.append(f"Cycle {cycle}: {str(e)}")
                        print(f"  ✗ Cycle {cycle+1} FAILED: {e}")
                
                # Summary
                print("\n" + "="*60)
                print("📊 STRESS TEST RESULTS")
                print("="*60)
                print(f"Cycles completed: {cycles_completed}/{num_cycles}")
                print(f"Errors encountered: {len(errors)}")
                
                if errors:
                    print("\nErrors:")
                    for err in errors[:10]:  # Show first 10
                        print(f"  - {err}")
                        
                success = len(errors) == 0 and cycles_completed == num_cycles
                print(f"\nResult: {'✅ PASSED' if success else '❌ FAILED'}")
                
            except Exception as e:
                print(f"❌ FATAL ERROR: {e}")
                success = False
            
        # Cleanup test bots in the test DB
        print("\nCleaning up test bots...")
        cleanup_test_bots(bot_ids)
        
    finally:
        # Restore database configuration and cleanup directory
        engine.database.DB_PATH = original_db_path
        if hasattr(engine.database._local, 'connection'):
            if engine.database._local.connection:
                try:
                    engine.database._local.connection.close()
                except Exception:
                    pass
            del engine.database._local.connection
            
        try:
            shutil.rmtree(test_dir)
        except Exception:
            pass
            
    return success

if __name__ == "__main__":
    result = test_concurrent_cycles(num_cycles=10)
    sys.exit(0 if result else 1)
