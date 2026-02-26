import sqlite3
from engine.database import DB_PATH

def fix_mismatch():
    print(f"Connecting to DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    bots_to_fix = [10001, 10002]
    
    for bot_id in bots_to_fix:
        # Get current state
        cursor.execute("SELECT b.name, t.total_invested, t.current_step FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.id=?", (bot_id,))
        row = cursor.fetchone()
        if row:
            name, invested, step = row
            print(f"Bot {bot_id} ({name}): Invested=${invested}, Step={step}")
            
            if invested > 0:
                print(f" -> FIXING Bot {bot_id}...")
                cursor.execute("UPDATE trades SET total_invested=0, current_step=1, avg_entry_price=0, entry_order_id=NULL WHERE bot_id=?", (bot_id,))
                
                # Also ensure bot is set to Scanning if active
                cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
                print(f" -> FIXED.")
            else:
                print(f" -> No fix needed (Invested is 0)")
        else:
            print(f"Bot {bot_id} not found.")

    conn.commit()
    conn.close()
    print("Database correction complete.")

if __name__ == "__main__":
    fix_mismatch()
