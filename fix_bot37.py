
from engine.database import get_connection

def fix_bot():
    print("Fixing Bot 37 state...")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id=37")
    conn.commit()
    print("Bot 37 reset to 'Waiting for Signal'.")
    conn.close()

if __name__ == "__main__":
    fix_bot()
