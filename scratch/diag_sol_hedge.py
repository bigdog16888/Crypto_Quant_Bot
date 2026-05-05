import sqlite3
import pandas as pd

def check_hedge_sol():
    conn = sqlite3.connect('crypto_bot.db')
    query = "SELECT * FROM bot_orders WHERE bot_id=100001 AND order_type IN ('hedge', 'hedge_tp')"
    df = pd.read_sql(query, conn)
    print(df.to_string(index=False))
    conn.close()

if __name__ == "__main__":
    check_hedge_sol()
