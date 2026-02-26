import sqlite3
import json
import os
import sys

# Add parent dir to path
sys.path.append(os.getcwd())
try:
    from engine.exchange_interface import ExchangeInterface
    from config.settings import config
except:
    print("Import failed, using simplified checking")

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def diagnose():
    print("--- MISMATCH DIAGNOSIS START ---", flush=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Get Bot 10000 Details
    print("\n[BOT 10000 DB STATE]", flush=True)
    cursor.execute("SELECT id, name, pair, direction FROM bots WHERE id=10000")
    bot = cursor.fetchone()
    if bot:
        print(f"  Bot: {bot[1]} ({bot[2]} {bot[3]})")
    
    cursor.execute("SELECT total_invested, entry_order_id, current_step FROM trades WHERE bot_id=10000")
    trade = cursor.fetchone()
    if trade:
        invested, entry_id, step = trade
        print(f"  Invested: ${invested:.2f}")
        print(f"  Entry Order ID: {entry_id}")
        print(f"  Step: {step}")
        
        # 2. Check Exchange for this specific ID
        print("\n[EXCHANGE ORDER CHECK]", flush=True)
        try:
            ex = ExchangeInterface(market_type='future')
            if entry_id:
                try:
                    order = ex.fetch_order(entry_id, 'BTC/USDC')
                    print(f"  Order {entry_id} Status: {order['status']}")
                    print(f"  Filled: {order['filled']}")
                    print(f"  Amount: {order['amount']}")
                except Exception as e:
                    print(f"  ❌ Order {entry_id} NOT FOUND on Exchange: {e}")
            else:
                print("  ⚠️ No Entry Order ID in DB!")
                
            # 3. Check Physical Position again
            print("\n[PHYSICAL POSITION CHECK]", flush=True)
            positions = ex.fetch_positions()
            btc_pos = next((p for p in positions if 'BTC' in p['symbol']), None)
            if btc_pos:
                print(f"  BTC Position: {btc_pos['side']} {btc_pos['contracts']} @ {btc_pos['entryPrice']}")
            else:
                print("  BTC Position: NONE")
                
        except Exception as ex_err:
            print(f"  Exchange Check Failed: {ex_err}")
            
    else:
        print("  Bot 10000 has no trade record.")

    conn.close()
    print("\n--- DIAGNOSIS COMPLETE ---", flush=True)

if __name__ == "__main__":
    diagnose()
