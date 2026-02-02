import sys
import os
import time

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner
from engine.database import get_connection, save_bot_order
from engine.bot_executor import BotExecutor

def run_verify():
    print("🚀 Starting Verification Round 4 (Self-Healing Cycle)")
    
    # Setup burner runner and executor
    runner = BotRunner()
    executor = runner._bot_executor or BotExecutor(runner)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # process_bot expects: bot_id, name, pair, direction, strategy_type, config, base_size, mm, rsi_limit, is_active
    columns = "id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active"
    
    # Process ALL active bots
    cursor.execute(f"SELECT {columns} FROM bots WHERE is_active = 1")
    bots = cursor.fetchall()
    
    for bot in bots:
        print(f"\n--- Testing Maintenance: {bot[1]} ({bot[2]}) ---")
        try:
            executor.process_bot(bot)
        except Exception as e:
            print(f"❌ Error processing {bot[1]}: {e}")

    print("\n--- Final Diagnostic Check ---")
    os.system("python tools/advanced_diagnostic.py")

if __name__ == "__main__":
    run_verify()
