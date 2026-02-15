"""Quick diagnostic: Compare DB state vs Exchange state"""
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

# DB State
conn = get_connection()
cur = conn.cursor()
print('=== DB BOTS IN TRADE ===')
for r in cur.execute('''
    SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price
    FROM bots b JOIN trades t ON b.id = t.bot_id
    WHERE t.total_invested > 0
''').fetchall():
    print(f'Bot {r[0]} ({r[1]}): {r[3]} on {r[2]} | ${r[4]:.2f} @ ${r[5]:.4f}')

# Exchange State
print('\n=== EXCHANGE POSITIONS ===')
ex = ExchangeInterface(market_type='future')
for pos in ex.exchange.fetch_positions():
    c = float(pos.get('contracts', 0) or 0)
    if c > 0:
        sym = pos.get('symbol')
        side = pos.get('side')
        entry = float(pos.get('entryPrice', 0) or 0)
        print(f'{sym}: {side} {c} @ ${entry:.2f}')

print('\n=== EXCHANGE OPEN ORDERS ===')
for o in ex.exchange.fetch_open_orders()[:10]:
    sym = o.get('symbol')
    side = o.get('side')
    amt = o.get('amount')
    price = o.get('price')
    otype = o.get('type')
    print(f'{sym}: {side} {amt} @ {price} ({otype})')
