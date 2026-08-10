#!/usr/bin/env python3
"""Fetch live exchange positions using ExchangeInterface with clean environment."""
import sys
import os
import json

# Force clean environment - no venv in path
os.environ['PYTHONPATH'] = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot'
sys.path = [
    r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot',
    r'C:\Users\Gionie\AppData\Local\Programs\Python\Python310\Lib',
    r'C:\Users\Gionie\AppData\Local\Programs\Python\Python310\DLLs',
]

from engine import database
from datetime import datetime

# First get active bots
database.DB_PATH = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
database.close_connection()
conn = database.get_connection()

rows = conn.execute('''
    SELECT b.id, b.name, b.pair, b.direction, b.status
    FROM bots b
    WHERE b.is_active = 1
    ORDER BY b.id
''').fetchall()

active_bots = []
for r in rows:
    active_bots.append({
        'id': r[0], 'name': r[1], 'pair': r[2], 
        'direction': r[3], 'status': r[4]
    })

database.close_connection()

print('=== ACTIVE BOTS ===')
for b in active_bots:
    print(f"  Bot {b['id']:6} | {b['name']:25} | {b['pair']:15} | {b['direction']:5} | {b['status']}")

# Now fetch live positions
print()
print('=== FETCHING LIVE EXCHANGE POSITIONS ===')
timestamp = datetime.now().isoformat()
print(f'Timestamp: {timestamp}')
print()

# Try importing ExchangeInterface with minimal imports
try:
    from engine.exchange_interface import ExchangeInterface
    ex = ExchangeInterface()
    positions = ex.fetch_positions()
    
    print(f'=== LIVE EXCHANGE POSITIONS @ {datetime.now().isoformat()} ===')
    for bot in active_bots:
        symbol = bot['pair'].split(':')[0]
        print(f'--- Bot {bot["id"]} ({bot["name"]}) | {bot["pair"]} | {bot["direction"]} ---')
        found = False
        for p in positions:
            pos_symbol = p['symbol'].split(':')[0]
            if pos_symbol == symbol:
                qty = float(p.get('contracts', p.get('net_qty', p.get('size', 0))) or 0)
                side = p.get('side', '').lower()
                if side == 'short' or qty < 0:
                    qty = -abs(qty)
                else:
                    qty = abs(qty)
                print(f'  Exchange: qty={qty:.8f} side={side}')
                found = True
                break
        if not found:
            print(f'  Exchange: NO POSITION FOUND (qty=0)')
        print()
    
    print(f'=== END FETCH @ {datetime.now().isoformat()} ===')
    
except Exception as e:
    print(f'ERROR fetching live positions: {e}')
    import traceback
    traceback.print_exc()