import sys
sys.path.insert(0, '.')
from engine.ledger import seal_all_active_bots
import sqlite3

DB_PATH = 'crypto_bot.db'

def run():
    print("Running seal_all_active_bots()...")
    res = seal_all_active_bots()
    print(f"Resealed/corrected count: {res}")
    
    # Now check status of all ETH bots
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = """
    SELECT b.id, b.name, b.is_active, b.status, b.bot_type,
           t.open_qty, t.total_invested, t.cycle_id, t.cycle_phase, t.entry_confirmed
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.pair LIKE '%ETH%USDC%'
    AND b.name NOT LIKE '%link%'
    ORDER BY b.name;
    """
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        print("\nColumns: id, name, is_active, status, bot_type, open_qty, total_invested, cycle_id, cycle_phase, entry_confirmed")
        for r in rows:
            print(r)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    run()
