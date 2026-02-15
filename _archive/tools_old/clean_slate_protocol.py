import sys
import os
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

def execute_clean_slate():
    print("="*60)
    print("🚀 EXECUTING CLEAN SLATE PROTOCOL")
    print("="*60)

    # 1. Close Exchange Position
    print("\n[1/3] Closing BTC Position on Exchange...")
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.fetch_positions()
        btc_pos = next((p for p in positions if 'BTC' in p['symbol'] and float(p['contracts']) > 0), None)
        
        if btc_pos:
            qty = float(btc_pos['contracts'])
            side = 'sell' if btc_pos['side'] == 'long' else 'buy'
            symbol = btc_pos['symbol']
            print(f"  Found position: {qty} {symbol} ({btc_pos['side']}). Closing...")
            
            # Execute Market Close
            try:
                ex.create_order(symbol, 'market', side, qty)
                print("  ✅ Position CLOSED on Exchange.")
            except Exception as order_err:
                print(f"  ❌ Failed to close position: {order_err}")
        else:
            print("  ℹ️ No BTC position found on exchange.")
            
    except Exception as e:
        print(f"  ❌ Error accessing exchange: {e}")

    # 2. Reset Database State (Bots 41 & 43)
    print("\n[2/3] Resetting Database State...")
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        bots_to_reset = [41, 43]
        
        for bot_id in bots_to_reset:
            print(f"  Resetting Bot {bot_id}...")
            # Remove trade record
            cursor.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
            # Close any open orders
            cursor.execute("UPDATE bot_orders SET status='closed' WHERE bot_id = ? AND status='open'", (bot_id,))
            
        conn.commit()
        conn.close()
        print("  ✅ Database state RESET.")
        
    except Exception as e:
        print(f"  ❌ Error resetting DB: {e}")

    print("\n[3/3] Protocol Complete.")

if __name__ == "__main__":
    execute_clean_slate()
