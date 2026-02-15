"""Quick check of exchange vs DB state"""
from engine.exchange_interface import ExchangeInterface
import sqlite3

print("=" * 60)
print("EXCHANGE POSITIONS")
print("=" * 60)
ex = ExchangeInterface(market_type='future')
positions = ex.fetch_positions()
active = [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]
for p in active:
    print(f"  {p.get('symbol')}: {p.get('side')} {p.get('contracts')} @ ${p.get('entryPrice')}")
print(f"Total: {len(active)} positions")

print("\n" + "=" * 60)
print("EXCHANGE ORDERS")
print("=" * 60)
orders = ex.fetch_open_orders()
for o in orders:
    print(f"  {o.get('symbol')}: {o.get('side')} {o.get('amount')} @ ${o.get('price')} | {o.get('clientOrderId', '')}")
print(f"Total: {len(orders) if orders else 0} orders")

print("\n" + "=" * 60)
print("DB BOTS IN TRADE")
print("=" * 60)
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('''
    SELECT t.bot_id, b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price 
    FROM trades t JOIN bots b ON t.bot_id = b.id 
    WHERE t.total_invested > 0
''')
rows = cur.fetchall()
for r in rows:
    print(f"  Bot {r[0]} ({r[1]}): Step {r[3]}, ${r[4]:.2f} @ ${r[5]:.2f}")
print(f"Total: {len(rows)} bots in trade")

# THE ISSUE: Are they all sharing the SAME position?
print("\n" + "=" * 60)
print("DIAGNOSIS")
print("=" * 60)
if len(active) < len(rows):
    print(f"⚠️ WARNING: {len(rows)} bots claim trades but only {len(active)} positions on exchange!")
    print("   This could mean multiple bots adopted the SAME position incorrectly.")
