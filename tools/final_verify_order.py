import os
import sys
import time
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner
from engine.bot_executor import BotExecutor
from engine.database import get_connection

def final_verify():
    runner = BotRunner()
    executor = BotExecutor(runner)
    
    # Target bot: btc long (ID 41)
    bot_id = 41
    conn = get_connection()
    cursor = conn.cursor()
    # Need 10: bot_id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit, is_active
    cursor.execute("SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active FROM bots WHERE id = ?", (bot_id,))
    bot_data = cursor.fetchone()
    
    if not bot_data:
        print("Bot 41 not found.")
        return

    print(f"🚀 FORCING ENTRY FOR {bot_data[1]}...")
    
    from engine.bot_executor import get_thread_exchange
    ex = get_thread_exchange('future')
    
    # 1. Execute Entry
    executor.execute_entry(bot_id, bot_data[1], bot_data[3], 'buy', bot_data[7], exchange=ex)
    
    print("Wait 5s for order to settle...")
    time.sleep(5)
    
    # 2. Run process_bot to trigger maintenance (Manage Trade)
    print("🛠️ RUNNING MAINTENANCE CYCLE...")
    executor.process_bot(bot_data)
    
    print("Wait 5s for orders to settle...")
    time.sleep(5)
    
    # 3. Check DB/Exchange state
    print("\n--- FINAL STATE CHECK ---")
    os.system("python tools/advanced_diagnostic.py")

if __name__ == "__main__":
    final_verify()
