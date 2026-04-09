import sqlite3
import sys, os
import time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # Check all XAU bot_orders
    print("=== XAU bot_orders (recent) ===")
    c.execute("""
        SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id 
        FROM bot_orders WHERE bot_id=10019 ORDER BY created_at DESC LIMIT 20
    """)
    for r in c.fetchall():
        print(r)
    
    print("\n=== XAU trades row ===")
    c.execute("SELECT * FROM trades WHERE bot_id=10019")
    row = c.fetchone()
    if row:
        print(row)
    conn.close()
    
    # Check open positions on exchange
    print("\n=== Physical positions from exchange ===")
    ex = ExchangeInterface('future')
    positions = ex.fetch_positions()
    for p in (positions or []):
        if 'XAU' in str(p.get('symbol', '')) or 'ETH' in str(p.get('symbol', '')):
            print(p)

check()
