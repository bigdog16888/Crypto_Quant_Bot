import sys
import os
import sqlite3
import time
import pandas as pd
from datetime import datetime

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")
LOG_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "engine.log")

def verify_system_health():
    print(f"\n{'='*50}")
    print(f"ROUND 8 SYSTEM VERIFICATION REPORT")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Check Active Bots & Traders
    print(">>> 1. ACTIVE BOTS & TRADES")
    cursor.execute('''
        SELECT count(*) FROM bots WHERE is_active = 1
    ''')
    active_bots = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT count(*) FROM trades WHERE total_invested > 0
    ''')
    active_trades = cursor.fetchone()[0]
    
    print(f"Active Bots (Config): {active_bots}")
    print(f"Bots in Trade (DB):   {active_trades}")
    
    # Detail for Bot 37
    cursor.execute('''
        SELECT b.id, b.name, b.is_active, t.total_invested 
        FROM bots b 
        LEFT JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = 37
    ''')
    b37 = cursor.fetchone()
    if b37:
        print(f"\n[Bot 37 Status]")
        print(f"Name: {b37[1]}")
        print(f"Active: {'YES' if b37[2] else 'NO'}")
        print(f"Invested: ${b37[3]}")
    else:
        print("\n[Bot 37 Status] NOT FOUND")

    # 2. Check Open Orders in DB
    print("\n>>> 2. OPEN ORDERS (DB View)")
    cursor.execute('''
        SELECT count(*) FROM bot_orders WHERE status = 'open'
    ''')
    open_orders = cursor.fetchone()[0]
    print(f"Total Open Orders: {open_orders}")
    
    if open_orders > 0:
        cursor.execute('''
            SELECT bot_id, order_type, price, amount FROM bot_orders WHERE status = 'open'
        ''')
        for o in cursor.fetchall():
            print(f" - Bot {o[0]} {o[1]}: {o[3]} @ {o[2]}")
    else:
        print(" - None")

    # 3. Check Recent Log Activity
    print("\n>>> 3. LOG HEALTH CHECK")
    if os.path.exists(LOG_PATH):
        try:
            # Read last 2KB
            with open(LOG_PATH, 'rb') as f:
                f.seek(0, os.SEEK_END)
                fsize = f.tell()
                f.seek(max(fsize - 5000, 0))
                lines = f.readlines()
            
            last_lines = [l.decode('utf-8', errors='ignore').strip() for l in lines[-10:]]
            
            # Extract timestamp from last line
            last_ts = "Unknown"
            if last_lines:
                # Assuming standard logging format: YYYY-MM-DD HH:MM:SS
                parts = last_lines[-1].split(',')
                if len(parts) > 0:
                    last_ts = parts[0]
            
            print(f"Log File Size: {fsize/1024/1024:.2f} MB")
            print(f"Last Log Entry: {last_ts}")
            
            # Check recency
            try:
                log_time = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
                diff = datetime.now() - log_time
                print(f"Time Since Last Log: {diff}")
                
                if diff.total_seconds() > 300: # 5 mins
                    print("⚠️ WARNING: Log appears stale (>5 mins). Engine may be DOWN or HUNG.")
                else:
                    print("✅ Log is fresh.")
            except:
                print("⚠️ Could not parse log timestamp.")
                
            print("\nRecent Errors/Warnings (Last 5000 bytes):")
            found_err = False
            for l in lines:
                decoded = l.decode('utf-8', errors='ignore')
                if "ERROR" in decoded or "CRITICAL" in decoded or "WARNING" in decoded:
                    print(f" > {decoded.strip()[:100]}...")
                    found_err = True
            if not found_err:
                 print(" - No recent errors found in tail.")
                 
        except Exception as e:
            print(f"Error reading log: {e}")
    else:
        print("Log file not found.")

    conn.close()
    print(f"\n{'='*50}")

if __name__ == "__main__":
    verify_system_health()
