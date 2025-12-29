import sqlite3
from engine.database import get_connection

def check_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    print("--- BOTS ---")
    cursor.execute("SELECT id, name, pair, is_active FROM bots")
    for row in cursor.fetchall():
        print(row)
        
    print("\n--- TRADES ---")
    cursor.execute("SELECT bot_id, avg_entry_price, target_tp_price, current_step FROM trades")
    for row in cursor.fetchall():
        print(row)
        
    conn.close()

if __name__ == "__main__":
    check_db()
