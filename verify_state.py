"""Verify bot state matches exchange"""
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

print('=== DB TRADE STATE ===')
conn = get_connection()
cur = conn.cursor()

db_btc_qty = 0
for row in cur.execute('''
    SELECT b.id, b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price
    FROM bots b JOIN trades t ON b.id = t.bot_id
    WHERE t.total_invested > 0
''').fetchall():
    bot_id, name, pair, step, invested, avg, tp = row
    qty = invested / avg if avg > 0 else 0
    print(f'Bot {bot_id} ({name}):')
    print(f'  Pair: {pair}')
    print(f'  Step: {step}, Invested: ${invested:.2f}')
    print(f'  Qty: {qty:.6f} @ ${avg:.2f}')
    print(f'  TP: ${tp:.2f}')
    if 'BTC' in pair:
        db_btc_qty += qty
    print()

print(f'DB TOTAL BTC: {db_btc_qty:.6f}')
print()

print('=== EXCHANGE STATE ===')
ex = ExchangeInterface(market_type='future')
positions = ex.exchange.fetch_positions()
ex_btc_qty = 0
for pos in positions:
    size = float(pos.get('contracts', 0) or 0)
    if size != 0:
        sym = pos.get('symbol')
        entry = float(pos.get('entryPrice', 0))
        print(f"{sym}: {size} @ ${entry:.2f}")
        if 'BTC' in sym:
            ex_btc_qty = size

print()
print(f'EXCHANGE BTC: {ex_btc_qty:.6f}')
print(f'DIFFERENCE: {db_btc_qty - ex_btc_qty:.6f} ({(db_btc_qty - ex_btc_qty) * 74000:.2f} USD)')

print()
print('=== OPEN ORDERS ===')
orders = ex.exchange.fetch_open_orders()
btc_tp_found = False
for o in orders:
    sym = o.get('symbol')
    side = o.get('side')
    amt = o.get('amount')
    price = o.get('price')
    print(f"{sym}: {side} {amt} @ {price}")
    if 'BTC' in sym and side == 'sell':
        btc_tp_found = True

if not btc_tp_found:
    print()
    print('⚠️ WARNING: No BTC TP order found on exchange!')
