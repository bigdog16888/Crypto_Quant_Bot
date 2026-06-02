import sqlite3
import pandas as pd

def run():
    conn = sqlite3.connect('crypto_bot.db')
    
    print("=== Bots directions ===")
    query = """
    SELECT id, name, direction, is_active, status
    FROM bots
    WHERE name LIKE '%eth%';
    """
    df = pd.read_sql_query(query, conn)
    print(df.to_string())
    
    conn.close()

if __name__ == '__main__':
    run()
