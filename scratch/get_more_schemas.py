import sqlite3

def get_more_schemas():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    for table in ['trade_history', 'reconciliation_logs']:
        cur.execute(f"PRAGMA table_info({table})")
        cols = cur.fetchall()
        print(f"Table: {table}")
        for col in cols:
            print(f"  {col[1]} ({col[2]})")
    conn.close()

if __name__ == '__main__':
    get_more_schemas()
