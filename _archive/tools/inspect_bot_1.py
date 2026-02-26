
import logging
import sys
import os

sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from engine.database import get_bot_status, get_connection
import pandas as pd

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Inspector")

BOT_ID = 1 # Assuming User meant Bot ID 1

def inspect_bot():
    print(f"🔍 Inspecting Bot {BOT_ID}...")
    
    # 1. DB State
    status = get_bot_status(BOT_ID)
    print(f"\n📊 DB Status:\n{status}")
    
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM bot_orders WHERE bot_id=? AND status='open'", (BOT_ID,))
    db_orders = c.fetchall()
    print(f"\n📂 DB Open Orders ({len(db_orders)}):")
    for o in db_orders:
        print(f" - {o}")
    conn.close()

    # 2. Exchange State
    # Need to know the pair to fetch orders.
    pair = status['pair'] if status else 'BTC/USDC' # Default fallback
    print(f"\n🌍 Fetching Exchange Orders for {pair}...")
    
    ex = ExchangeInterface(market_type='future') # Assuming future
    orders = ex.fetch_open_orders(pair)
    
    bot_orders = [o for o in orders if o.get('clientOrderId', '').startswith(f'CQB_{BOT_ID}_')]
    
    print(f"\n🔴 Live Exchange Orders ({len(bot_orders)}):")
    for o in bot_orders:
        print(f" - ID: {o['id']} | Type: {o['clientOrderId']} | Status: {o['status']} | Amount: {o['amount']} | Filled: {o['filled']}")

if __name__ == "__main__":
    inspect_bot()
