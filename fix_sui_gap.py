import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

# Get exchange position truth
ex = ExchangeInterface()
positions = ex.fetch_positions()
sui_pos = next((p for p in positions if 'SUI' in p.get('symbol', '')), None)

if sui_pos:
    qty = abs(sui_pos.get('contracts', 0))
    entry_px = sui_pos.get('entryPrice', 0)
    notional = qty * entry_px
    print(f"Exchange SUI position: qty={qty}, entryPrice={entry_px}, notional=${notional:.2f}")
else:
    print("No SUI position on exchange")
    qty, entry_px, notional = 0, 0, 0

conn = get_connection()
c = conn.cursor()

# 1. Remove the GAP_REPAIR dummy entry
c.execute("DELETE FROM bot_orders WHERE bot_id=10018 AND (order_id LIKE 'GAP_REPAIR%' OR client_order_id LIKE '%REPAIR%')")
deleted = c.rowcount
print(f"\nDeleted {deleted} GAP_REPAIR entries from bot_orders")

# 2. Set total_invested and avg_entry_price to exactly match exchange
if notional > 0:
    c.execute("UPDATE trades SET total_invested=?, avg_entry_price=? WHERE bot_id=10018", (notional, entry_px))
    print(f"Updated trades: total_invested=${notional:.2f}, avg_entry_price={entry_px:.6f}")
else:
    print("WARNING: No exchange position found, manual fix needed")

conn.commit()
conn.close()
print("Done.")
