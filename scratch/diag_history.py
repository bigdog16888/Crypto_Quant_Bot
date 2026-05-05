import sqlite3
import pandas as pd

def check_history():
    conn = sqlite3.connect('crypto_bot.db')
    # Get table info to be sure about column names
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(trade_history)")
    cols = [c[1] for c in cursor.fetchall()]
    print(f"Columns: {cols}")
    
    query = "SELECT * FROM trade_history WHERE symbol LIKE '%XAU%' OR symbol LIKE '%SOL%' ORDER BY id DESC LIMIT 50"
    df = pd.read_sql(query, conn)
    print(df.to_string(index=False))
    conn.close()

if __name__ == "__main__":
    check_history()
