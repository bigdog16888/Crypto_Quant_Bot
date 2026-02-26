import os
import sqlite3
import pandas as pd

def check_recent_history():
    db_path = os.path.join(os.path.dirname(__file__), 'crypto_bot.db')
    conn = sqlite3.connect(db_path)
    
    # We need to trace EXACTLY what the bot believes it owns vs what it actually owns.
    
    query = """
    SELECT bot_id, total_invested, avg_entry_price, current_step,
           (total_invested / avg_entry_price) as btc_size_implied
    FROM trades
    WHERE bot_id IN (10002, 10004, 10015)
    """
    df_trades = pd.read_sql(query, conn)
    print("--- VIRTUAL TRADES (BTC SIZE) ---")
    print(df_trades)
    print(f"Total Implied BTC: {df_trades['btc_size_implied'].sum():.4f}\n")
    
    print("--- RECENT FILLS FOR ACTIVE BOTS OUT OF SYNC? ---")
    query_history = """
    SELECT bot_id, action, amount, price, datetime(timestamp, 'unixepoch', 'localtime') as time
    FROM trade_history
    WHERE bot_id IN (10002, 10004, 10015)
    ORDER BY timestamp DESC
    LIMIT 20
    """
    print(pd.read_sql(query_history, conn))

if __name__ == "__main__":
    check_recent_history()
