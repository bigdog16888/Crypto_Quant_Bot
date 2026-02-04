import sqlite3
import json
import time

DB_PATH = 'crypto_bot.db'

def check_cooldown(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get config
    cursor.execute("SELECT config, pair FROM bots WHERE id=?", (bot_id,))
    row = cursor.fetchone()
    if not row:
        print("Bot not found")
        return
    
    config = json.loads(row[0])
    pair = row[1]
    
    # Get status
    cursor.execute("SELECT last_exit_price, last_exit_time FROM trades WHERE bot_id=?", (bot_id,))
    status = cursor.fetchone()
    
    last_exit_price = status[0]
    last_exit_time = status[1]
    
    print(f"Bot {bot_id} ({pair})")
    print(f"Last Exit Price: {last_exit_price}")
    print(f"Last Exit Time: {last_exit_time} (Delta: {(time.time() - last_exit_time)/60:.2f} mins ago)")
    
    reentry_mins = config.get('reentry_cooldown_mins', 0)
    reentry_dist = config.get('reentry_distance_pct', 0.0)
    
    print(f"Config: Cooldown {reentry_mins} mins, Distance {reentry_dist}%")
    
    can_enter = True
    
    if last_exit_time > 0 and reentry_mins > 0:
        if (time.time() - last_exit_time) / 60 < reentry_mins:
            print(f"❌ BLOCKED by Cooldown (Waited {(time.time() - last_exit_time)/60:.2f} < {reentry_mins})")
            can_enter = False
            
    if last_exit_price > 0 and reentry_dist > 0:
        # Need current price?
        pass # Can't check easily without fetching price, but let's assume price is ~78000
        
    if can_enter:
        print("✅ Cooldown Check Passed (Time-wise)")

if __name__ == "__main__":
    check_cooldown(37)
