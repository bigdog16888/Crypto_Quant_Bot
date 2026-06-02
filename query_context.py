import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
db = sqlite3.connect('crypto_bot.db')
db.row_factory = sqlite3.Row

# Import target functions
import sys
sys.path.append('.')
from engine.database import recompute_invested_from_orders

print("=== Recompute for bot 100318 (current database state) ===")
res = recompute_invested_from_orders(100318)
print(f"Result: {res}")

# Let's inspect the trades table row for 100318
print("\n=== trades row ===")
trades_row = db.execute("SELECT * FROM trades WHERE bot_id = 100318").fetchone()
print(dict(trades_row))

# Let's manually run the query from recompute_invested_from_orders
print("\n=== Simulating recompute SQL query ===")
target_cycle = trades_row['cycle_id']
wall_ts = trades_row['wipe_wall_ts']
bot_side = trades_row['position_side']
bot_id = 100318

print(f"Params: cycle_id={target_cycle}, wall_ts={wall_ts}, bot_side={bot_side}, bot_id={bot_id}")

query = """
    SELECT 
        bo.id, bo.order_type, bo.price, bo.filled_amount, bo.status, bo.cycle_id, bo.position_side, bo.created_at
    FROM bot_orders bo
    WHERE bo.bot_id = ?
      AND (
          bo.position_side = ? 
          OR bo.position_side IS NULL 
          OR bo.position_side = 'BOTH' 
          OR bo.position_side = ''
      )
      AND (
          bo.status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled')
          OR (bo.status IN ('canceled', 'cancelled') AND bo.filled_amount > 0)
      )
      AND bo.filled_amount > 0
"""
rows = db.execute(query, (bot_id, bot_side)).fetchall()
for r in rows:
    print(dict(r))

db.close()
