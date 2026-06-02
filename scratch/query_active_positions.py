import sqlite3
import pandas as pd

def run():
    conn = sqlite3.connect('crypto_bot.db')
    
    print("=== Table Schema: active_positions ===")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(active_positions)")
    print(cursor.fetchall())
    
    print("\n=== Contents of active_positions ===")
    df = pd.read_sql_query("SELECT * FROM active_positions", conn)
    print(df.to_string())
    
    conn.close()

if __name__ == '__main__':
    run()
