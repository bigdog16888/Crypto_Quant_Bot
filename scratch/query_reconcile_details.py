import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM reconciliation_logs WHERE bot_id = 10018 ORDER BY timestamp DESC LIMIT 5")
    rows = cur.fetchall()
    cur.execute("PRAGMA table_info(reconciliation_logs)")
    cols = [c[1] for c in cur.fetchall()]
    for r in rows:
        print("Reconciliation Log:")
        for col, val in zip(cols, r):
            print(f"  {col}: {val}")
    conn.close()

if __name__ == '__main__':
    run()
