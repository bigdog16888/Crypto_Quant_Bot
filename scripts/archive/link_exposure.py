import sqlite3, pandas as pd
from engine.exchange_interface import ExchangeInterface
import logging
logging.basicConfig(level=logging.WARNING)

conn = sqlite3.connect('crypto_bot.db')

print("=== Query 1: Bot + Trade State ===")
df = pd.read_sql_query('''
SELECT b.id, b.name, b.direction, b.pair,
       t.open_qty, t.total_invested, t.avg_entry_price,
       t.cycle_id, t.current_step, t.tp_order_id,
       t.position_side, t.cycle_phase
FROM bots b JOIN trades t ON t.bot_id = b.id
WHERE b.id = 10020;
''', conn)
print(df.to_string(index=False))

print("\n=== Query 2: Live Exchange Position ===")
ex = ExchangeInterface()
phys = ex.fetch_positions()
for p in phys:
    if 'LINK' in p['symbol']:
        print(f"  symbol:        {p['symbol']}")
        print(f"  side:          {p['side']}")
        print(f"  contracts:     {p['contracts']}")
        print(f"  entryPrice:    {p['entryPrice']}")
        print(f"  unrealizedPnl: {p['unrealizedPnl']}")

print("\n=== Query 3: Open Orders on Exchange for LINK ===")
try:
    open_orders = ex.fetch_open_orders('LINK/USDC:USDC')
    if open_orders:
        for o in open_orders:
            print(f"  id={o.get('id')} type={o.get('type')} side={o.get('side')} amount={o.get('amount')} price={o.get('price')} status={o.get('status')}")
    else:
        print("  (no open orders)")
except Exception as e:
    print(f"  fetch_open_orders failed: {e}")

print("\n=== Query 4: LINK current price ===")
try:
    price = ex.get_last_price('LINK/USDC:USDC')
    print(f"  Last price: {price}")
except Exception as e:
    print(f"  get_last_price failed: {e}")
