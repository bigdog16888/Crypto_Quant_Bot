"""
Verify: what the health check actually compares.
- System: trades.total_invested (cost basis)
- Exchange: position_amt * current_price (mark-to-market)

XRP bot 10017:
  total_qty = 823.4 units
  exchange notional = $1,069.75
  implied current price = 1069.75 / 823.4 = $1.2992

Let's also verify against what monitor.py / health_check uses.
"""
import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
c = conn.cursor()

# Current DB state
c.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=10017")
r = c.fetchone()
db_invested = r[0]
avg_price = r[1]

# Get position from exchange
ex = ExchangeInterface()
positions = ex.fetch_positions()
xrp_pos = next((p for p in positions if 'XRP' in p.get('symbol','')), None)

print(f"DB total_invested: ${db_invested:.2f}")
print(f"DB avg_entry_price: {avg_price:.6f}")
print()
if xrp_pos:
    qty = abs(xrp_pos.get('contracts', 0))
    entry_px = xrp_pos.get('entryPrice', 0)
    print(f"Exchange position qty: {qty}")
    print(f"Exchange entry price: {entry_px}")
    exchange_notional = qty * entry_px
    print(f"Exchange notional (qty * entryPrice): ${exchange_notional:.2f}")
else:
    print("No XRP position found on exchange")

conn.close()
