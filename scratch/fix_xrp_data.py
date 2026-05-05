from engine.database import get_connection
import time

def fix():
    conn = get_connection()
    cur = conn.cursor()
    # Fix the missing timestamp for the netting order to ensure it's counted in the current cycle
    cur.execute("UPDATE bot_orders SET created_at = 1777949662 WHERE id = 97112")
    conn.commit()
    print("Fixed XRP netting order timestamp.")

if __name__ == "__main__":
    fix()
