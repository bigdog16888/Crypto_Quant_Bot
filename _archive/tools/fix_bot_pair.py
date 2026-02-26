
from engine.database import get_connection

def fix_bot_pair():
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE bots SET pair='BTC/USDC' WHERE id=10001")
    conn.commit()
    print("✅ updated Bot 10001 to BTC/USDC")

if __name__ == "__main__":
    fix_bot_pair()
