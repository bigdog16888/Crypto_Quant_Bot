import sqlite3
import os

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def dump_trades():
    print(f"--- DUMPING TRADES from {DB_PATH} ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        entry = cursor.execute("SELECT t.bot_id, t.total_invested, b.pair, b.direction, b.name FROM trades t JOIN bots b ON t.bot_id = b.id").fetchall()
        
        total_sys = 0.0
        print(f"{'BotID':<10} {'Pair':<10} {'Dir':<6} {'Invested':<15} {'Name'}")
        print("-" * 60)
        for row in entry:
            bid, inv, pair, direct, name = row
            inv = float(inv)
            total_sys += inv
            print(f"{bid:<10} {pair:<10} {direct:<6} {inv:<15.2f} {name}")
            
        print("-" * 60)
        print(f"TOTAL SYSTEM INVESTED: ${total_sys:,.2f}")
        
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    dump_trades()
