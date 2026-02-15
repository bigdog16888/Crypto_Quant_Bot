import sys
import os
import logging
import time

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner
from engine.database import get_connection, get_bot_status

# Configure logging to console specifically for this test
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("DebugCycle")

def run_one_cycle():
    print("\n⚡ STARTING SINGLE LIVE CYCLE EXECUTION ⚡")
    print("===========================================")
    
    # 1. Initialize Runner
    runner = BotRunner()
    
    # Force markets to load
    if runner.exchange is not None:
        print("   ...Initializing Exchange & Markets...")
        runner.exchange._ensure_markets()
    
    # 2. Run ONE Cycle
    print("   ...Executing Logic Loop (This may take 10-20s)...")
    runner.run_cycle()
    
    print("\n===========================================")
    print("🏁 CYCLE COMPLETE. ANALYZING RESULTS.")
    
    # 3. Analyze What Happened
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check Trades
    cursor.execute("""
        SELECT b.name, b.pair, t.total_invested, t.entry_confirmed 
        FROM trades t JOIN bots b ON t.bot_id = b.id
    """)
    trades = cursor.fetchall()
    
    print("\n📈 TRADES TRIGGERED:")
    if trades:
        for t in trades:
            print(f"   🚀 BOT: {t[0]} | PAIR: {t[1]} | INVESTED: ${t[2]:.2f}")
    else:
        print(f"   (No trades triggered this cycle)")

    print("\n===========================================")

if __name__ == "__main__":
    run_one_cycle()
