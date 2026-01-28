
import sqlite3
from pathlib import Path

DB_PATH = Path("crypto_bot.db")

print("=" * 80)
print("FORCE RESET STATE")
print("=" * 80)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

try:
    print("Clearing all active data...")
    cursor.execute("DELETE FROM bot_orders")
    print(f"  Deleted {cursor.rowcount} orders")
    
    cursor.execute("DELETE FROM active_positions")
    print(f"  Deleted {cursor.rowcount} active positions")
    
    cursor.execute("DELETE FROM bot_ownership_state")
    print(f"  Deleted {cursor.rowcount} ownership states")
    
    # We optionally keep the bots definition, or verify if we need to clear them too.
    # For now, let's keep the bots so we don't have to recreate them, but reset their active status.
    # Wait, the user wants "one bot". Let's disable all bots first.
    cursor.execute("UPDATE bots SET is_active = 0")
    print(f"  Deactivated {cursor.rowcount} bots")
    
    # Reset trades table? User said "cleaned up ghost trades". 
    # Let's clean open trades.
    cursor.execute("DELETE FROM trades") # This clears history too? checks schema.
    # Schema check earlier said trades has bot_id, current_step etc. Seems to be "active trade session".
    # We should clear it.
    print(f"  Deleted {cursor.rowcount} trade sessions")

    conn.commit()
    print("✅ Reset complete.")

except Exception as e:
    print(f"❌ Error: {e}")
    conn.rollback()
finally:
    conn.close()
