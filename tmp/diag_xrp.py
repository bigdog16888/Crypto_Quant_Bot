import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    ex = ExchangeInterface('future')
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    print("=== XAU Position ===")
    positions = ex.fetch_positions()
    for p in (positions or []):
        if 'XAU' in str(p.get('symbol', '')):
            print(p)
            
    print("\n=== XRP BOTS ===")
    c.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%XRP%'")
    for r in c.fetchall():
        print(r)
        c.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=?", (r[0],))
        print("  Trades row:", c.fetchone())
        
    print("\n=== XRP bot_orders (recent filled) ===")
    c.execute("""
        SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id, cycle_id, bot_id
        FROM bot_orders 
        WHERE order_id IS NOT NULL AND status != 'canceled' AND filled_amount > 0 AND bot_id IN (
            SELECT id FROM bots WHERE pair LIKE '%XRP%'
        )
        ORDER BY created_at DESC LIMIT 20
    """)
    for r in c.fetchall():
        print(r)
        
    c.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair LIKE '%XRP%'")
    print("\n=== XRP active_positions ===")
    for r in c.fetchall():
        print(r)

    conn.close()

if __name__ == '__main__':
    check()
