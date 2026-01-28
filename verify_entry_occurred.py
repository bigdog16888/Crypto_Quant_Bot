
import sqlite3
import time
import sys

def verify():
    # 1. Check DB
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE bot_id=43")
    trades = cur.fetchall()
    conn.close()
    
    print(f"--- Checking Bot 43 Trades ---")
    if trades:
        print(f"✅ Trade Found in DB! Count: {len(trades)}")
        for t in trades:
            print(f"Trade: Step {t[1]}, Entry Cost: {t[4]}")
    else:
        print("❌ No trades in DB yet.")
        
    # 2. Check recent logs for success/failure
    print("\n--- Checking Recent Logs ---")
    enc = 'utf-8' # or 'latin-1' if needed
    try:
        with open('engine.log', 'r', encoding=enc) as f:
            lines = f.readlines()[-100:] # Last 100 lines
            
        found_entry = False
        for line in lines:
            if "Entry finalized" in line and "long btc price" in line:
                print(f"✅ LOG CONFIRMATION: {line.strip()}")
                found_entry = True
            elif "ERROR" in line and "BotExecutor" in line:
                 print(f"⚠️ RECENT ERROR: {line.strip()}")
                 
        if not found_entry and not trades:
            print("⏳ Bot appears to be scanning (or still failing silently). Check settings.")
            
    except Exception as e:
        print(f"Log check failed: {e}")

if __name__ == "__main__":
    verify()
