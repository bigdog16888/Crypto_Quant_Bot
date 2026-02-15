import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from engine.database import get_connection

def cleanup_test_bots():
    conn = get_connection()
    cursor = conn.cursor()
    
    bots_to_delete = ['Test_Bot_A', 'Test_Bot_B', 'Test_Bot_C']
    for bot_name in bots_to_delete:
        print(f"Deleting {bot_name}...")
        cursor.execute("SELECT id FROM bots WHERE name = ?", (bot_name,))
        res = cursor.fetchone()
        if res:
            bot_id = res[0]
            # Delete trade history
            cursor.execute("DELETE FROM trade_history WHERE bot_id = ?", (bot_id,))
            # Delete active trades
            cursor.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
            # Delete orders
            cursor.execute("DELETE FROM bot_orders WHERE bot_id = ?", (bot_id,))
            # Delete bot
            cursor.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            print(f"Deleted Bot {bot_id}: {bot_name}")
        else:
            print(f"Bot {bot_name} not found.")
    
    conn.commit()
    print("Cleanup complete.")

if __name__ == "__main__":
    cleanup_test_bots()
