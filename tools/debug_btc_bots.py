import sqlite3
import os

DB_PATH = "crypto_bot.db"

def query():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get all columns for context
    cursor.execute("""
        SELECT b.id, b.name, b.pair, t.current_step, t.total_invested, t.target_tp_price, b.is_active 
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1 AND b.pair LIKE '%BTC%'
    """)
    rows = cursor.fetchall()
    
    print(f"{'ID':<3} | {'Name':<20} | {'Pair':<10} | {'Step':<4} | {'Invested':<10} | {'TP':<10} | {'Active':<8}")
    print("-" * 75)
    for r in rows:
        print(f"{r[0]:<3} | {r[1]:<20} | {r[2]:<10} | {r[3] if r[3] is not None else 'N/A':<4} | {r[4] if r[4] is not None else 0.0:<10.2f} | {r[5] if r[5] is not None else 0.0:<10.2f} | {r[6]:<8}")
    
    conn.close()

if __name__ == "__main__":
    query()
