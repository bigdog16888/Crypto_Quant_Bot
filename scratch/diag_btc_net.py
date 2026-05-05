import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath("."))

from engine.database import get_pair_virtual_net

db_path = "crypto_bot.db"
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit()

pair = 'BTC/USDC:USDC'
virtual_net = get_pair_virtual_net(pair)
print(f"Pair: {pair}")
print(f"Virtual Net (from database helper): {virtual_net}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("\n--- BTCUSDC BOTS (Active) ---")
cur.execute("""
    SELECT b.id, b.name, b.direction, t.open_qty 
    FROM bots b JOIN trades t ON b.id=t.bot_id 
    WHERE b.pair=? AND b.is_active=1
""", (pair,))
for row in cur.fetchall():
    print(row)

print("\n--- BTCUSDC BOT_ORDERS Net (Current Cycles) ---")
# This mimics the logic in get_pair_virtual_net
cur.execute("""
    SELECT bo.bot_id, 
           SUM(CASE WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount ELSE 0 END) -
           SUM(CASE WHEN bo.order_type IN ('tp', 'exit', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN bo.filled_amount ELSE 0 END) as bot_net
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair = ? AND bo.status IN ('filled', 'closed')
    GROUP BY bo.bot_id
""", (pair,))
for row in cur.fetchall():
    print(row)

print("\n--- BTCUSDC HEDGES Net ---")
cur.execute("""
    SELECT bo.bot_id, b.direction,
           SUM(CASE WHEN bo.order_type = 'hedge' THEN bo.filled_amount ELSE 0 END) -
           SUM(CASE WHEN bo.order_type = 'hedge_tp' THEN bo.filled_amount ELSE 0 END) as hedge_net
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair = ? AND bo.status IN ('filled', 'closed', 'hedge_exited', 'reset_cleared', 'auto_closed')
    GROUP BY bo.bot_id
""", (pair,))
for row in cur.fetchall():
    print(row)

conn.close()
