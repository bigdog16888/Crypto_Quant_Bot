from engine.database import get_connection

def fix():
    conn = get_connection()
    cur = conn.cursor()
    # Void the phantom adoption that was pushed into a new cycle without physical backing
    cur.execute("UPDATE bot_orders SET status = 'reset_cleared' WHERE id = 97260")
    conn.commit()
    print("Voided ghost XRP adoption.")

if __name__ == "__main__":
    fix()
