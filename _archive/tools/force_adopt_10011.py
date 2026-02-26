
import sqlite3
import sys
import os
import time

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.reconciler import StateReconciler, ReconciliationAction
from engine.database import update_bot_status

def force_adoption():
    print("🚀 FORCE ADOPTION: Bot 10011 -> 0.151 BTC")
    
    # Direct DB Injection because we KNOW the truth from the Audit
    # Audit: 3 trades, total 0.151 BTC, Avg Entry 62892.90
    
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # 1. Ensure Bot is Active
    cursor.execute("UPDATE bots SET is_active=1, status='In Trade' WHERE id=10011")
    
    # 2. Inject Trade Record
    # We use the AUDITED values:
    # Size: 0.151 BTC * 66866.92 (approx market) or use Audit Entry?
    # Audit Entry: 62892.90
    # Total Invested = 0.151 * 62892.90 = 9496.8279
    # Current Market Value ~ 10096.90
    
    invested = 0.151 * 62892.90
    entry_price = 62892.90
    
    cursor.execute("""
        INSERT OR REPLACE INTO trades 
        (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time)
        VALUES (10011, 2, ?, ?, 1, ?)
    """, (invested, entry_price, int(time.time())))
    
    conn.commit()
    print(f"✅ INJECTED: 0.151 BTC @ {entry_price} (Invested ${invested:.2f})")
    
    # 3. Log it
    cursor.execute("""
        INSERT INTO reconciliation_logs (timestamp, bot_id, pair, action, details, proof_order_id)
        VALUES (?, 10011, 'BTC/USDC', 'FORCE_ADOPTION_AUDIT', 'Manually injected audited position (step 2, 0.151 BTC)', 'AUDIT_MANUAL')
    """, (int(time.time()),))
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    force_adoption()
