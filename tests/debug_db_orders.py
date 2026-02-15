
import sqlite3
import pandas as pd
import time

DB_PATH = 'c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db'

def inspect_orders():
    conn = sqlite3.connect(DB_PATH)
    try:
        print("--- OPEN ORDERS IN DB ---")
        df = pd.read_sql_query("SELECT * FROM bot_orders WHERE status='open'", conn)
        if not df.empty:
            print(df.to_string())
        else:
            print("No open orders found.")
            
        print("\n--- POSITIONS IN DB ---")
        # Manually fix state
        print("FIXING STATE...")
        conn.execute("UPDATE trades SET total_invested = 139.5, avg_entry_price = 69500, entry_confirmed = 1, current_step = 1, basket_start_time = ? WHERE bot_id IN (10000, 10001)", (int(time.time()),))
        conn.commit()
        
        df_trades = pd.read_sql_query("SELECT * FROM trades WHERE total_invested > 0", conn)
        if not df_trades.empty:
            print(df_trades.to_string())
        else:
            print("No open trades found.")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

def clear_orders():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM bot_orders WHERE status='open'")
        # Reset active trades
        cursor.execute("UPDATE trades SET total_invested=0, current_step=0, entry_order_id=NULL WHERE total_invested > 0")
        conn.commit()
        print("\n✅ Cleared all open orders and reset trades from DB.")
    except Exception as e:
        print(f"Error clearing DB: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    inspect_orders()
    # Uncomment to clear
    clear_orders()
