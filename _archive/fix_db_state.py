"""Fix DB trade state to match exchange position - using engine.database"""
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

# Get exchange BTC position
ex = ExchangeInterface(market_type='future')
positions = ex.exchange.fetch_positions()

btc_pos = None
for pos in positions:
    if 'BTC' in pos.get('symbol', ''):
        btc_pos = pos
        break

if btc_pos:
    total_contracts = float(btc_pos.get('contracts', 0))
    entry_price = float(btc_pos.get('entryPrice', 0))
    
    # Split evenly
    split_qty = total_contracts / 2
    split_value = split_qty * entry_price
    tp_price = entry_price * 1.015
    
    print(f'Exchange: {total_contracts} BTC @ ${entry_price:.2f}')
    print(f'Each bot: {split_qty:.6f} BTC = ${split_value:.2f}')
    
    # Use engine.database connection
    conn = get_connection()
    
    # Check current state first
    print('\nBEFORE:')
    for row in conn.execute('SELECT bot_id, current_step, total_invested, entry_confirmed FROM trades WHERE bot_id IN (41, 43)').fetchall():
        print(f'  Bot {row[0]}: step={row[1]}, invested=${row[2]:.2f}, entry_confirmed={row[3]}')
    
    # Bot 41: max_steps=10
    conn.execute('''
        UPDATE trades SET 
            current_step = 10,
            total_invested = ?,
            avg_entry_price = ?,
            target_tp_price = ?,
            entry_confirmed = 1
        WHERE bot_id = 41
    ''', (split_value, entry_price, tp_price))
    
    # Bot 43: max_steps=7 
    conn.execute('''
        UPDATE trades SET 
            current_step = 7,
            total_invested = ?,
            avg_entry_price = ?,
            target_tp_price = ?,
            entry_confirmed = 1
        WHERE bot_id = 43
    ''', (split_value, entry_price, tp_price))
    
    conn.commit()
    
    print('\nAFTER:')
    for row in conn.execute('SELECT bot_id, current_step, total_invested, entry_confirmed FROM trades WHERE bot_id IN (41, 43)').fetchall():
        print(f'  Bot {row[0]}: step={row[1]}, invested=${row[2]:.2f}, entry_confirmed={row[3]}')
    
    print('\n✅ Fixed both bots!')
else:
    print('No BTC position found on exchange!')
