import sqlite3
import os

DB_PATH = 'crypto_bot.db'

def clean_ghosts():
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print("🔍 Searching for GHOST/USDT bots...")
    c.execute("SELECT id, name, pair, status FROM bots WHERE pair LIKE '%GHOST%'")
    rows = c.fetchall()
    
    if rows:
        for r in rows:
            print(f"🗑️ Deleting GHOST Bot: ID={r[0]} Name={r[1]} Pair={r[2]}")
            c.execute("DELETE FROM bots WHERE id=?", (r[0],))
            c.execute("DELETE FROM trades WHERE bot_id=?", (r[0],))
            c.execute("DELETE FROM trade_history WHERE bot_id=?", (r[0],))
    else:
        print("✅ No GHOST bots found in DB.")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    clean_ghosts()
