import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.database import get_connection

def heal_stuck_bots():
    """
    Finds bots that are in a trade but have a target_tp_price of 0.0
    and recalculates/saves the correct TP price to un-stuck them.
    This version performs a direct calculation based on known strategy logic
    to avoid instantiating the full BotRunner.
    """
    print("--- Starting Healing Script for Stuck Bots ---")
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.bot_id, t.avg_entry_price, b.direction, b.config 
        FROM trades t 
        JOIN bots b ON t.bot_id = b.id 
        WHERE t.total_invested > 0 AND (t.target_tp_price IS NULL OR t.target_tp_price = 0.0)
    """)
    stuck_bots = cursor.fetchall()

    if not stuck_bots:
        print("✅ No stuck bots found.")
        conn.close()
        return

    print(f"Found {len(stuck_bots)} stuck bot(s). Healing now...")

    for bot_id, entry_price, direction, config_json in stuck_bots:
        try:
            params = json.loads(config_json) if config_json else {}
            # Replicate the core logic of MartingaleStrategy.calculate_tp_price
            tp_percent = params.get('tp_percent', 1.5)
            
            if direction.upper() == 'LONG':
                tp_price = float(entry_price) * (1 + (tp_percent / 100.0))
            else: # SHORT
                tp_price = float(entry_price) * (1 - (tp_percent / 100.0))

            cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (tp_price, bot_id))
            conn.commit()
            print(f"  ✅ Bot {bot_id}: Healed. Set target_tp_price to {tp_price:.4f}")

        except Exception as e:
            print(f"  ❌ Bot {bot_id}: FAILED to heal. Reason: {e}")
            conn.rollback()

    conn.close()
    print("--- Healing complete. ---")

if __name__ == "__main__":
    heal_stuck_bots()
