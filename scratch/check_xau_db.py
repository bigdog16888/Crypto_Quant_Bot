import sqlite3
import os

db_path = "crypto_bot.db"
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit()

conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("--- XAUUSDT BOTS ---")
cur.execute("""
    SELECT b.id, b.name, b.direction, t.total_invested, t.avg_entry_price, t.open_qty 
    FROM bots b JOIN trades t ON b.id=t.bot_id 
    WHERE b.pair='XAU/USDT:USDT' AND b.is_active=1
""")
for row in cur.fetchall():
    print(row)

print("\n--- XAUUSDT HEDGES ---")
cur.execute("""
    SELECT bo.bot_id, bo.order_type, bo.filled_amount, bo.status 
    FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id 
    WHERE b.pair='XAU/USDT:USDT' AND bo.order_type LIKE 'hedge%'
""")
for row in cur.fetchall():
    print(row)

conn.close()
