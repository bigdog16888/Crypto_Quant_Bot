from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection
import sqlite3
import os

def check_status():
    print("--- [1] Checking Open Orders (XAU/USDT) ---")
    try:
        ex = ExchangeInterface(market_type='future')
        orders = ex.fetch_open_orders('XAU/USDT')
        print(f"Count: {len(orders)}")
        for o in orders:
            print(f"  ID: {o['id']} | Type: {o['type']} | Side: {o['side']} | $ {o['price']} | Tag: {o.get('clientOrderId')}")
    except Exception as e:
        print(f"Exchange Error: {e}")

    print("\n--- [2] Checking DB State (Bot 44) ---")
    if os.path.exists('crypto_bot.db'):
        try:
            conn = sqlite3.connect('crypto_bot.db')
            c = conn.cursor()
            c.execute("SELECT Total_Invested, Current_Step FROM trades WHERE bot_id=44")
            row = c.fetchone()
            if row:
                print(f"Invested: {row[0]}, Step: {row[1]}")
            else:
                print("No active trade in DB (Clean)")
        except Exception as e:
            print(f"DB Error: {e}")
    else:
        print("crypto_bot.db not found!")
        
    print("\n--- [3] Checking for Zombie DB ---")
    if os.path.exists('trading_bot.db'):
        print("⚠️ trading_bot.db REAPPEARED!")
    else:
        print("✅ trading_bot.db is gone.")

if __name__ == "__main__":
    check_status()
