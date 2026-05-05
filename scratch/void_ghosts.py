from engine.database import get_connection
import time

def void_ghosts():
    conn = get_connection()
    cur = conn.cursor()
    ids = (97248, 97239)
    print(f"Voiding ghosts: {ids}")
    cur.execute("UPDATE bot_orders SET status='reset_cleared', updated_at=? WHERE id IN (?, ?)", (int(time.time()), *ids))
    conn.commit()
    print("Done.")

if __name__ == "__main__":
    void_ghosts()
