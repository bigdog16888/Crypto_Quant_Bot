import sqlite3
import pandas as pd

def check_btc_history():
    print("--- 🔍 BTC/USDC HISTORY CHECK ---")
    conn = sqlite3.connect('crypto_bot.db')
    
    # Check if any filled orders exist for BTC/USDC bots (32-36)
    query = """
    SELECT bot_id, SUM(amount) as total_bought, COUNT(*) as order_count
    FROM bot_orders 
    WHERE bot_id IN (32,33,34,35,36) 
    AND status = 'filled'
    AND order_type IN ('entry', 'grid')
    GROUP BY bot_id
    """
    df = pd.read_sql_query(query, conn)
    
    if df.empty:
        print("   No filled orders found for Bots 32-36.")
    else:
        print(df.to_string(index=False))
        
    # Check general trades table too
    print("\n[Trades Table]")
    df_trades = pd.read_sql_query("SELECT * FROM trades WHERE bot_id IN (32,33,34,35,36)", conn)
    print(df_trades if not df_trades.empty else "   No trade rows for these bots.")
    
    conn.close()

if __name__ == "__main__":
    check_btc_history()
