import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# What is the current cycle_id?
c.execute("SELECT cycle_id FROM trades WHERE bot_id=10011")
cycle_id = c.fetchone()[0]
print(f"Current cycle_id: {cycle_id}")

# ALL cycle 4 orders with ANY fill
print("\n=== ALL cycle 4 bot_orders (incl zero-filled) ===")
c.execute("""
    SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id, cycle_id
    FROM bot_orders 
    WHERE bot_id=10011 AND cycle_id=4
    ORDER BY created_at ASC
""")
for r in c.fetchall():
    print(r)

# Now check if any open orders ON EXCHANGE have partial fills we missed
print("\n=== PASS-1 check: fetch open order from exchange ===")
c.execute("""
    SELECT order_id, client_order_id, order_type, amount, filled_amount, status
    FROM bot_orders 
    WHERE bot_id=10011 AND cycle_id=4 AND order_id IS NOT NULL
""")
rows = c.fetchall()
conn.close()

ex = ExchangeInterface('future')
for row in rows:
    oid, cid, otype, amt, filled, status = row
    try:
        result = ex.fetch_order(str(oid), 'ETHUSDC')
        exchange_filled = float(result.get('filled', 0))
        exchange_status = result.get('status', '')
        diff = abs(exchange_filled - (filled or 0))
        flag = " <-- MISMATCH" if diff > 0.0001 else ""
        print(f"  OID={oid} [{otype}] DB_filled={filled} Exchange_filled={exchange_filled} Status={exchange_status}{flag}")
    except Exception as e:
        print(f"  OID={oid} [{otype}] Error: {e}")
