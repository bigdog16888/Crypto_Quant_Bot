import sqlite3
import os
import sys

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection

def check_ownership():
    print("🔐 CHECKING OWNERSHIP TABLE")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT bot_id, is_owner, state, pair FROM bot_ownership_state")
    rows = cursor.fetchall()
    
    for r in rows:
        print(f"   Bot {r[0]}: is_owner={r[1]}, state={r[2]}, pair={r[3]}")

if __name__ == "__main__":
    check_ownership()
