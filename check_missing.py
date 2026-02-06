"""Check for missing orders - exchange has way more than DB tracks"""
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
cur = conn.cursor()

print('=== FILLED ORDERS IN bot_orders (All) ===')
total_qty = 0
for r in cur.execute("""
    SELECT bot_id, order_type, price, amount, status
    FROM bot_orders
    WHERE status = 'filled' 
    AND (order_type = 'entry' OR order_type = 'grid')
    ORDER BY bot_id, created_at
""").fetchall():
    bot_id, otype, price, amt, status = r
    cost = price * amt
    total_qty += amt
    print(f'Bot {bot_id}: {otype:5} | {amt:.6f} @ {price:.2f} = ${cost:.2f}')

print(f'\nTOTAL QTY IN DB: {total_qty:.6f}')
print(f'EXCHANGE QTY:    0.578000')  
print(f'GAP:             {0.578 - total_qty:.6f} ({(0.578 - total_qty) * 74000:.2f} USD)')
print(f'\nThis gap represents orders that were filled but never saved to bot_orders!')

print('\n=== CHECKING EXCHANGE ORDER HISTORY ===')
ex = ExchangeInterface(market_type='future')
print('Fetching last 50 trades from exchange...')
trades = ex.exchange.fetch_my_trades('BTC/USDC:USDC', limit=50)
ex_total = sum(float(t.get('amount', 0)) for t in trades if t.get('side') == 'buy')
print(f'Exchange recent BUY trades: {ex_total:.6f} BTC')
