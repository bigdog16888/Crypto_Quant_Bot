import sqlite3
import pandas as pd

def search_resets():
    conn = sqlite3.connect('crypto_bot.db')
    query = "SELECT * FROM trade_history WHERE notes LIKE '%System reset%' ORDER BY timestamp DESC"
    df = pd.read_sql(query, conn)
    
    from datetime import datetime
    df['time_str'] = df['timestamp'].apply(lambda x: datetime.fromtimestamp(int(x)).strftime('%Y-%m-%d %H:%M:%S'))
    
    print(df[['id', 'time_str', 'symbol', 'action', 'notes']].to_string(index=False))
    conn.close()

if __name__ == "__main__":
    search_resets()
