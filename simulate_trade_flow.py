
import sqlite3
import time
from pathlib import Path

# Connect to DB
conn = sqlite3.connect("crypto_bot.db")
cursor = conn.cursor()

def run():
    print("--- SIMULATING ONE BOT TRADING ---")
    
    # 1. Create or Get One Bot (clean slate)
    # Check if a test bot exists
    cursor.execute("SELECT id FROM bots WHERE name='TestBot_Verification' LIMIT 1")
    res = cursor.fetchone()
    if res:
        bot_id = res[0]
        print(f"Using existing Bot ID: {bot_id}")
    else:
        print("Creating new Test Bot...")
        cursor.execute("INSERT INTO bots (name, pair, direction, strategy_type, is_active, config, rsi_limit, martingale_multiplier, base_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ('TestBot_Verification', 'BTC/USDT', 'LONG', 'MQL4', 1, '{}', 70.0, 1.5, 10.0))
        bot_id = cursor.lastrowid
        print(f"Created Bot ID: {bot_id}")
    
    conn.commit()

    # 2. Simulate Entry (Insert into trades table to say 'we are in trade')
    # Use schema we found: bot_id, current_step, total_invested..
    print(f"Putting Bot {bot_id} into trade...")
    
    # Insert initial trade record
    cursor.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time, target_tp_price)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, 1, 100.0, 50000.0, 1, int(time.time()), 51000.0))
    
    # Insert active position check
    cursor.execute("""
        INSERT INTO active_positions (pair, side, size, entry_price, owner_bot_id, last_updated)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ('BTC/USDT', 'LONG', 0.002, 50000.0, bot_id, int(time.time())))
    
    conn.commit()
    print("Bot is now 'In Trade'.")
    
    # Note: In a real scenario, the 'engine' loop would see this and generate orders.
    # Since we aren't running the full engine loop here, we might need to manually trigger the logic 
    # OR the user expects us to *verify* what the engine does. 
    # If the user's engine is NOT running, nothing will happen.
    # I will assume the user wants me to *simulate* the state so they can turn on the engine OR 
    # I should try to run the order generation logic once.
    
    print("Done. Please start the engine or run the order manager manually.")

run()
conn.close()
