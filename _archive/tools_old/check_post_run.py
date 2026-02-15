import sqlite3
import pandas as pd
import os

def check_test_bots():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # Check Bots Status
    print("\n--- Test Bot Status ---")
    cursor.execute("SELECT id, name, is_active, status FROM bots WHERE id >= 10000")
    bots = cursor.fetchall()
    for b in bots:
        print(f"Bot {b[0]} ({b[1]}): Active={b[2]}, Status='{b[3]}'")
        
    # Check for Ghost Kills in Logs
    print("\n--- Recent Ghost Logs ---")
    cursor.execute("SELECT timestamp, type, message FROM notifications WHERE message LIKE '%Ghost%' OR message LIKE '%RESET%' ORDER BY timestamp DESC LIMIT 5")
    logs = cursor.fetchall()
    if logs:
        for l in logs:
            print(f"[{l[0]}] {l[1]}: {l[2]}")
    else:
        print("No Ghost/Reset notifications found.")

    # Check Trade History for Resets
    print("\n--- Recent Trade History (Resets) ---")
    cursor.execute("SELECT timestamp, action, notes FROM trade_history WHERE action='GHOST_RESET' ORDER BY timestamp DESC LIMIT 5")
    resets = cursor.fetchall()
    if resets:
        for r in resets:
            print(f"[{r[0]}] {r[1]}: {r[2]}")
    else:
        print("No GHOST_RESET actions found in trade history.")

if __name__ == "__main__":
    check_test_bots()
