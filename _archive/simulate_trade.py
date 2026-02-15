import sqlite3
from engine.database import get_connection

def simulate_trade(bot_name, entry, tp):
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get Bot ID
    cursor.execute("SELECT id FROM bots WHERE name = ?", (bot_name,))
    bot = cursor.fetchone()
    
    if bot:
        bot_id = bot[0]
        # Update Trades table
        # Ensure row exists (add_bot creates it)
        cursor.execute("""
            UPDATE trades 
            SET avg_entry_price = ?, target_tp_price = ?, current_step = 1 
            WHERE bot_id = ?
        """, (entry, tp, bot_id))
        
        if cursor.rowcount == 0:
            print(f"No trade row found for bot {bot_id}?")
        else:
            print(f"Updated Bot {bot_name} (ID {bot_id}): Entry={entry}, TP={tp}")
        
        conn.commit()
    else:
        print(f"Bot {bot_name} not found.")
    
    conn.close()

if __name__ == "__main__":
    simulate_trade("PaperTestBot", 87200.00, 87400.00)
