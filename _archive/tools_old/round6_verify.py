import os
import sys
import time
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner
from engine.bot_executor import BotExecutor, get_thread_exchange
from engine.database import get_connection

def round6_verify():
    print("🚀 STARTING VERIFICATION ROUND 6: MULTI-BOT STABILITY")
    runner = BotRunner()
    executor = BotExecutor(runner)
    
    # Target bots: btc long (41) and gold long (31)
    # Note: IDs might vary, so we'll fetch them by name to be sure
    bot_names = ['btc long', 'gold long']
    conn = get_connection()
    cursor = conn.cursor()
    
    bots_to_test = []
    for name in bot_names:
        cursor.execute("SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active FROM bots WHERE name = ?", (name,))
        bot = cursor.fetchone()
        if bot:
            bots_to_test.append(bot)
        else:
            print(f"⚠️ Bot '{name}' not found - skipping.")

    if not bots_to_test:
        print("❌ No bots found for testing!")
        return

    # 1. Force Entry for all bots
    for bot_data in bots_to_test:
        bot_id, name, pair, direction = bot_data[0], bot_data[1], bot_data[2], bot_data[3]
        print(f"\n👉 FORCING ENTRY FOR {name} ({pair})...")
        ex = get_thread_exchange('future')
        # base_size is at index 6 in this query
        executor.execute_entry(bot_id, name, pair, 'buy' if direction == 'LONG' else 'sell', bot_data[6], exchange=ex)

    print("\n⏳ Waiting 8s for entries to settle...")
    time.sleep(8)

    # 2. Run maintenance cycle for each
    print("\n🛠️ RUNNING MAINTENANCE CYCLES...")
    for bot_data in bots_to_test:
        print(f"Processing bot: {bot_data[1]}...")
        executor.process_bot(bot_data)

    print("\n⏳ Waiting 8s for Grid/TP orders to settle...")
    time.sleep(8)

    # 3. Final Diagnostic Report
    print("\n--- ROUND 6: FINAL DIAGNOSTIC REPORT ---")
    os.system("python tools/advanced_diagnostic.py")
    
    # 4. Success check in logs
    print("\n--- FINAL LOG AUDIT (Looking for -1104, -1106, or TypeErrors) ---")
    os.system("powershell -Command \"Get-Content engine.log -Tail 30\"")

if __name__ == "__main__":
    round6_verify()
