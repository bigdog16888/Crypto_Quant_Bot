import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

print("=== Query: BTC + XAU bot states ===")
df = pd.read_sql_query('''
SELECT b.id, b.name, b.direction, b.pair,
       t.open_qty, t.total_invested, t.cycle_id, t.wipe_wall_ts
FROM bots b JOIN trades t ON t.bot_id = b.id
WHERE b.pair IN ('BTC/USDC:USDC','XAU/USDT:USDT') AND b.is_active = 1;
''', conn)
print(df.to_string(index=False))

print("\n=== Exchange: BTC + XAU live positions ===")
from engine.exchange_interface import ExchangeInterface
import logging
logging.basicConfig(level=logging.WARNING)
ex = ExchangeInterface()
phys = ex.fetch_positions()
for p in phys:
    if any(sym in p['symbol'] for sym in ['BTC', 'XAU']) and p.get('contracts', 0) > 0:
        print(f"  {p['symbol']:20s}  side={p['side']:5s}  contracts={p['contracts']:10}  entry={p['entryPrice']}  pnl={p['unrealizedPnl']:.4f}")

print("\n=== Last fills per BTC/XAU bot (to confirm ownership) ===")
df2 = pd.read_sql_query('''
SELECT bo.bot_id, b.name, bo.order_type, bo.filled_amount, bo.status, bo.cycle_id, bo.created_at
FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id
WHERE b.pair IN ('BTC/USDC:USDC','XAU/USDT:USDT')
  AND bo.filled_amount > 0
  AND bo.status NOT IN ('cancelled','canceled','failed')
ORDER BY bo.bot_id, bo.created_at DESC
LIMIT 20;
''', conn)
print(df2.to_string(index=False))
