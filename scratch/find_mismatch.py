import sqlite3
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def check_all_positions():
    ex = ExchangeInterface()
    positions = ex.fetch_positions()
    print("=== LIVE EXCHANGE POSITIONS ===")
    for p in positions:
        print(f"Symbol: {p['symbol']}, side: {p['side']}, net_qty: {p['net_qty']}, contracts: {p['contracts']}, entryPrice: {p['entryPrice']}")
        
    print("\n=== SYSTEM BOTS & OPEN QTY ===")
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT b.id, b.name, b.normalized_pair, b.direction, b.bot_type, t.open_qty
        FROM trades t JOIN bots b ON b.id = t.bot_id
        WHERE t.open_qty != 0 OR b.normalized_pair LIKE '%SUI%'
    """)
    for r in cursor.fetchall():
        print(r)
    conn.close()

if __name__ == '__main__':
    check_all_positions()
