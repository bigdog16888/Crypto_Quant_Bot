import sqlite3

def check_active_positions():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM active_positions")
    rows = cur.fetchall()
    print("Physical Positions from active_positions table:")
    if not rows:
        print("  Empty")
    else:
        # Get column names
        cur.execute("PRAGMA table_info(active_positions)")
        cols = [c[1] for c in cur.fetchall()]
        for r in rows:
            print("  ---")
            for col, val in zip(cols, r):
                print(f"    {col}: {val}")
    conn.close()

if __name__ == '__main__':
    check_active_positions()
