import sqlite3
import os
import sys

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection

def fix_db_values():
    print("🛠️ FIXING DB VALUES TO MATCH EXCHANGE REALITY...")
    
    # 1. Update BTC Trade (Bot 43)
    # 0.002 * 69317.8 = 138.6356
    real_btc_invested = 138.6356
    
    # 2. Update Gold Trade (Bot 44)
    # 0.001 * 5035.97 = 5.03597
    real_gold_invested = 5.03597
    
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Update Trades
        cursor.execute("UPDATE trades SET total_invested=? WHERE bot_id=43 AND total_invested > 0", (real_btc_invested,))
        print(f"   ✅ Updated Bot 43 (BTC) to ${real_btc_invested:.4f}")
        
        cursor.execute("UPDATE trades SET total_invested=? WHERE bot_id=44 AND total_invested > 0", (real_gold_invested,))
        print(f"   ✅ Updated Bot 44 (Gold) to ${real_gold_invested:.4f}")
        
        # Update Ownership State (for consistency)
        cursor.execute("UPDATE bot_ownership_state SET position_size=? WHERE bot_id=43", (real_btc_invested,))
        cursor.execute("UPDATE bot_ownership_state SET position_size=? WHERE bot_id=44", (real_gold_invested,))
        print("   ✅ Updated Ownership Records")
        
        conn.commit()
        print("🎉 DB Correction Complete.")
        
    except Exception as e:
        print(f"❌ Failed to update DB: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    fix_db_values()
