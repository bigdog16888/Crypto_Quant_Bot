
import sqlite3
import time

def clean_ghosts():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # 1. Clear Bot 10001 (Ghost Short)
    print("Clearing Bot 10001 (Short Ghost)...")
    c.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0 WHERE bot_id=10001")
    c.execute("UPDATE bots SET status='Scanning' WHERE id=10001")
    
    # 2. Clear Bot 10015 (Mock Ghost)
    print("Clearing Bot 10015 (Mock Ghost)...")
    c.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0 WHERE bot_id=10015")
    c.execute("UPDATE bots SET status='Scanning' WHERE id=10015")
    
    conn.commit()
    print("✅ Ghosts Busted.")
    conn.close()

if __name__ == "__main__":
    clean_ghosts()
