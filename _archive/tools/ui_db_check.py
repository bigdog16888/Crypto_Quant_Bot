import sqlite3
import pandas as pd
import os

DB_PATH = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

def main():
    print(f"Checking DB at {DB_PATH}")
    print(f"File exists: {os.path.exists(DB_PATH)}")
    print(f"File size: {os.stat(DB_PATH).st_size}")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Check trades with invested > 0
    query = "SELECT bots.id, bots.name, trades.total_invested FROM trades JOIN bots ON trades.bot_id = bots.id WHERE total_invested > 0"
    df = pd.read_sql(query, conn)
    print("\nACTIVE TRADES (Invested > 0):")
    print(df)
    
    # Check mismatch bots specifically
    query = "SELECT id, name, status FROM bots WHERE id IN (10001, 10002)"
    df_bots = pd.read_sql(query, conn)
    print("\nBOT STATUSES (10001, 10002):")
    print(df_bots)
    
    conn.close()

if __name__ == "__main__":
    main()
