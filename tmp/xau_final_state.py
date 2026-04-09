import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    """
    Fetch the XAU bot's virtual state vs physical 
    to determine the correct TP over-buy gap.
    """
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    
    # Check what bot 10019 virtual ledger shows
    c.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=10019")
    t = c.fetchone()
    print(f"XAU bot virtual: invested={t[0]}, avg_price={t[1]}, step={t[2]}, cycle={t[3]}")
    
    # Physical position
    ex = ExchangeInterface('future')
    positions = ex.fetch_positions()
    for p in (positions or []):
        if 'XAU' in str(p.get('symbol', '')):
            print(f"Physical: {p}")
            
    conn.close()

if __name__ == '__main__':
    check()
