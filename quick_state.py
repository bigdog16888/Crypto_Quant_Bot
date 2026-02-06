"""Quick check after reset"""
from engine.exchange_interface import ExchangeInterface
import sqlite3

ex = ExchangeInterface(market_type='future')

print("POSITIONS:")
positions = ex.fetch_positions()
for p in positions:
    contracts = float(p.get('contracts', 0) or 0)
    if abs(contracts) > 0:
        print(f"  {p.get('symbol')}: {p.get('side')} {contracts}")

print("\nORDERS:")
orders = ex.fetch_open_orders()
for o in orders or []:
    print(f"  {o.get('symbol')}: {o.get('id')}")

print("\nDB BOTS:")
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
print(f"  In trade: {cur.fetchone()[0]}")
conn.close()
