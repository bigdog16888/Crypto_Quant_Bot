import sqlite3
import os
import time
import datetime

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def monitor():
    print(f"--- MONITORING {DB_PATH} ---")
    last_count = -1
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM active_positions")
            count = cursor.fetchone()[0]
            
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            
            if count != last_count:
                print(f"[{ts}] Count changed: {count}")
                if count > 0:
                    cursor.execute("SELECT * FROM active_positions")
                    rows = cursor.fetchall()
                    for r in rows:
                        print(f"  > {r}")
                last_count = count
            
            conn.close()
        except Exception as e:
            print(f"Error: {e}")
        
        time.sleep(1)

if __name__ == "__main__":
    monitor()
