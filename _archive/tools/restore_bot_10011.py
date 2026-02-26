import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from config.settings import config
from engine.database import DB_PATH

def restore_bot_10011():
    print(f"Connecting to DB at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if already exists
        cursor.execute("SELECT id FROM bots WHERE id = 10011")
        if cursor.fetchone():
            print("Bot 10011 already exists. Skipping.")
            return

        # Insert Bot 10011
        # ID: 10011
        # Name: Grid_Recovery_Bot
        # Pair: BTC/USDC
        # Direction: SHORT (matching the rogue position)
        print("Restoring Bot 10011...")
        cursor.execute("""
            INSERT INTO bots (id, name, pair, direction, strategy_type, is_active, status, config, rsi_limit, martingale_multiplier, base_size)
            VALUES (10011, 'Grid_Recovery_Bot', 'BTC/USDC', 'SHORT', 'grid', 1, 'Scanning', '{}', 0, 0, 0)
        """)
        
        conn.commit()
        print("✅ Bot 10011 Restored successfully.")
        
    except Exception as e:
        print(f"❌ Failed to restore bot: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    restore_bot_10011()
