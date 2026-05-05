import sqlite3
import json
import sys

def run_query(sql, params=()):
    conn = sqlite3.connect('crypto_bot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python query.py \"SQL QUERY\"")
        sys.exit(1)
    
    query = sys.argv[1]
    # Simple param handling if needed, but for now just raw query
    results = run_query(query)
    print(json.dumps(results, indent=2))
