import sqlite3
import pandas as pd

def run():
    conn = sqlite3.connect('crypto_bot.db')
    
    print("=== Trade History for Bot 10021 ===")
    query = """
    SELECT * FROM trade_history
    WHERE bot_id = 10021
    ORDER BY timestamp DESC LIMIT 10;
    """
    df = pd.read_sql_query(query, conn)
    print(df.to_string())
    
    conn.close()

if __name__ == '__main__':
    run()
