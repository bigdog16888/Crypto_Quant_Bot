import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
c = conn.cursor()

c.execute("SELECT b.id, b.name, b.direction, t.total_invested, t.avg_entry_price, t.current_step FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%LINK%'")
link_bots = c.fetchall()

print("=== LINK DB State ===")
total_sys_val = 0
for r in link_bots:
    bid, name, direction, inv, entry, step = r
    qty = inv / entry if entry > 0 else 0
    sys_val = inv  # notional
    sign = -1 if direction.upper() == 'SHORT' else 1
    total_sys_val += sign * sys_val
    print(f"  Bot {bid} ({name}): Dir={direction}, Invested=${inv:.2f}, Entry={entry:.4f}, Qty={qty:.4f}, Step={step}")

print(f"\n  Net System Notional: ${total_sys_val:.2f}")

ex = ExchangeInterface()
positions = ex.fetch_positions()
print("\n=== LINK Exchange State ===")
ex_val = 0
for p in positions:
    if 'LINK' in p['symbol']:
        qty = p['contracts']
        entry = p['entryPrice']
        val = abs(qty * entry)
        side = p['side']
        sign = -1 if side.upper() == 'SHORT' else 1
        ex_val += sign * val
        print(f"  {p['symbol']}: {side}, {qty} contracts @ {entry:.4f} = ${val:.2f}")

print(f"  Net Exchange Notional: ${ex_val:.2f}")
print(f"\n  DIFF: ${abs(total_sys_val) - abs(ex_val):.2f}")
print("  (Positive diff = system thinks it has MORE than exchange)")

conn.close()
