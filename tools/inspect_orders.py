import sqlite3
import pandas as pd

def inspect_orders(bot_id):
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print(f"--- Inspecting Orders for Bot {bot_id} ---")
    
    # Get all open orders
    query = f"SELECT order_id, type, side, price, amount, created_at, status FROM bot_orders WHERE bot_id={bot_id} AND status='open'"
    df = pd.read_sql_query(query, conn)
    
    if df.empty:
        print("No open orders found.")
    else:
        print(df)
        
        # Check for duplicates (same price/side/type)
        dupes = df[df.duplicated(subset=['price', 'side', 'type'], keep=False)]
        if not dupes.empty:
            print("\n❌ POTENTIAL DUPLICATES FOUND:")
            print(dupes)
        else:
            print("\n✅ No obvious duplicates (by Price/Side/Type).")

    conn.close()

if __name__ == "__main__":
    inspect_orders(43)
