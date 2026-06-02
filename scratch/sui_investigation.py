import engine.database
from engine.exchange_interface import ExchangeInterface
from config.settings import config
import json

conn = engine.database.get_connection()

print("--- Query 1: bot_orders query ---")
q1 = """
SELECT order_type, status, filled_amount, amount, price, cycle_id, datetime(created_at, 'unixepoch', 'localtime') as created_at_dt, client_order_id
FROM bot_orders
WHERE bot_id = (SELECT id FROM bots WHERE name = 'sui long')
AND filled_amount > 0
AND status IN ('cancelled', 'auto_closed', 'reset_cleared', 'partially_filled')
ORDER BY created_at DESC LIMIT 20;
"""
rows = conn.execute(q1).fetchall()
for r in rows:
    print(r)

print("\n--- Query 2: trades query ---")
q2 = """
SELECT open_qty, total_invested, avg_entry_price, cycle_id
FROM trades WHERE bot_id = (SELECT id FROM bots WHERE name = 'sui long');
"""
trade_row = conn.execute(q2).fetchone()
print(trade_row)

print("\n--- Live Exchange Check ---")
try:
    # Initialize exchange using standard future interface
    exchange = ExchangeInterface(market_type='future')
    positions = exchange.fetch_positions()
    for p in positions:
        print(p['symbol'], p.get('side'), p.get('contracts'), p.get('entryPrice'))
except Exception as e:
    print("Exchange check error:", e)
