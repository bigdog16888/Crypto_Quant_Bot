"""Deep comparison of DB vs Exchange orders for Bot 37"""
import sqlite3
from engine.exchange_interface import ExchangeInterface

# Get DB orders
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('SELECT order_type, order_id, status, client_order_id FROM bot_orders WHERE bot_id = 37 ORDER BY id')
db_orders = cur.fetchall()

print("=" * 70)
print("BOT 37 - DATABASE ORDERS")
print("=" * 70)
for o in db_orders:
    print(f"  Type: {o[0]:10} | ID: {o[1]:15} | Status: {o[2]:8} | ClientID: {o[3]}")
print(f"Total: {len(db_orders)}")

# Get exchange orders
print("\n" + "=" * 70)
print("BOT 37 - EXCHANGE ORDERS (tagged CQB_37_*)")
print("=" * 70)
ex = ExchangeInterface(market_type='future')
orders = ex.fetch_open_orders()
bot37_orders = [o for o in orders if o.get('clientOrderId', '').startswith('CQB_37_')]
for o in bot37_orders:
    print(f"  ID: {o.get('id'):15} | ClientID: {o.get('clientOrderId'):30} | Price: ${o.get('price')}")
print(f"Total: {len(bot37_orders)}")

# The problem
print("\n" + "=" * 70)
print("PROBLEM")
print("=" * 70)
db_ids = set(o[1] for o in db_orders)
ex_ids = set(o.get('id') for o in bot37_orders)
missing_in_db = ex_ids - db_ids
print(f"Orders on EXCHANGE but NOT in DB: {missing_in_db}")
print(f"This means {len(missing_in_db)} orders were placed but never saved to bot_orders table!")
