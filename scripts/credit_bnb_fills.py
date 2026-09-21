import os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.database import get_connection, record_exchange_fill
from engine.ledger import credit_fill, seal_trade_state

conn = get_connection()
now = int(time.time())

fills_to_credit = [
    {
        'exchange_order_id': '350133162',
        'client_order_id': '',
        'symbol': 'BNB/USDC:USDC',
        'side': 'SELL',
        'qty': 0.01,
        'price': 710.5,
        'fill_ts': now,
        'source': 'reconciler-uncredited',
        'bot_id': 10007,
        'order_type': 'grid',
        'step': 27,
        'cycle_id': 27,
    },
    {
        'exchange_order_id': '350133201',
        'client_order_id': '',
        'symbol': 'BNB/USDC:USDC',
        'side': 'SELL',
        'qty': 0.01,
        'price': 711.94,
        'fill_ts': now,
        'source': 'reconciler-uncredited',
        'bot_id': 10007,
        'order_type': 'grid',
        'step': 27,
        'cycle_id': 27,
    },
]

print('=== CREDITING 2 BNB GRID FILLS FOR BOT 10007 ===')
for f in fills_to_credit:
    success = record_exchange_fill(conn=conn, **f)
    status = 'INSERTED' if success else 'DUPLICATE/SKIP'
    print(f"  order_id={f['exchange_order_id']:>12} side={f['side']} qty={f['qty']} price={f['price']} -> {status}")
    if success:
        credit_fill(
            bot_id=f['bot_id'],
            order_id=f['exchange_order_id'],
            cumulative_qty=f['qty'],
            avg_price=f['price'],
            order_type=f['order_type'],
            is_cumulative=True,
            caller=f['source'],
            side=f['side'],
        )
        seal_trade_state(f['bot_id'])

# Update active_positions for bot 10007: size=0.03, last_checked = now+1 (excludes these fills from future delta)
conn.execute(
    "UPDATE active_positions SET size = 0.03, last_checked = ?, last_updated = datetime('now') WHERE bot_id = 10007",
    (now + 1,)
)
conn.commit()
print(f'  active_positions updated: bot 10007 size=0.03, last_checked={now+1}')

# Update trades for bot 10007: open_qty=0.03
conn.execute("UPDATE trades SET open_qty = 0.03 WHERE bot_id = 10007")
conn.commit()
print('  trades updated: bot 10007 open_qty=0.03')

# Verify
c = conn.cursor()
ap = c.execute('SELECT bot_id, side, size, last_checked FROM active_positions WHERE bot_id = 10007').fetchone()
tr = c.execute('SELECT bot_id, open_qty FROM trades WHERE bot_id = 10007').fetchone()
print(f'\n=== VERIFICATION ===')
print(f'  active_positions: bot={ap[0]} side={ap[1]} size={ap[2]} last_checked={ap[3]}')
print(f'  trades:           bot={tr[0]} open_qty={tr[1]}')

conn.close()
print('\nBNB position reconciled: virtual = physical = -0.03.')
