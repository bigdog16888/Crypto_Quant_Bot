import sys; sys.path.append('.')
from engine.exchange_interface import ExchangeInterface
import config.settings as s
import sqlite3

try:
    ex = ExchangeInterface(market_type='future')
    pos = ex.fetch_positions()
    print('\n=== PHYSICAL POSITIONS (EXCHANGE) ===')
    for p in pos:
        size = float(p.get('contracts', 0) or 0)
        if abs(size) > 0:
            print(f"{p.get('symbol')} {p.get('side', '')}: {size} @ {p.get('entryPrice')}")
            
    print('\n=== VIRTUAL POSITIONS (DB) ===')
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    c.execute('SELECT b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.is_active=1')
    for name, pair, d, inv, avg in c.fetchall():
        if inv and avg and float(avg) > 0:
            qty = float(inv) / float(avg)
            if qty > 0:
                print(f"{name} ({pair} {d}): {qty:.5f} @ {avg:.5f} (Inv: ${inv:.2f})")
except Exception as e:
    print(f'Error: {e}')
