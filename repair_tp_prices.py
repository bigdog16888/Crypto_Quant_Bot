import os
import sys
import json
import sqlite3
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path: sys.path.append(BASE_DIR)

from engine.database import get_connection
from engine.runner import BotRunner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('TP_Repair')

def repair_tp_prices():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all active in-trade bots
    cursor.execute("""
        SELECT b.id, b.name, b.direction, b.config, t.total_invested, t.avg_entry_price, t.current_step, t.target_tp_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1 AND t.total_invested > 0
    """)
    bots = cursor.fetchall()
    
    from engine.strategies.martingale_strategy import MartingaleStrategy
    repaired_count = 0
    
    for row in bots:
        bot_id, name, direction, config_json, invested, avg_entry, step, current_tp = row
        bot_params = json.loads(config_json) if config_json else {}
        strategy = MartingaleStrategy(bot_params)
        
        bot_status = {
            'avg_entry_price': avg_entry,
            'total_invested': invested,
            'current_step': step
        }
        
        # Calculate correct TP
        correct_tp = strategy.calculate_take_profit_price(bot_status=bot_status, current_price=avg_entry)
        
        if abs(correct_tp - current_tp) > (avg_entry * 0.0001):  # If different by more than 0.01%
            logger.info(f"Bot {bot_id} ({name} {direction}): TP Mismatch! Current DB TP: {current_tp:.4f} -> Corrected TP: {correct_tp:.4f} (Entry: {avg_entry:.4f})")
            
            # Update DB
            cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (correct_tp, bot_id))
            
            # Delete old TP order from bot_orders so executor recreates it
            cursor.execute("DELETE FROM bot_orders WHERE bot_id = ? AND order_type IN ('tp_order', 'take_profit', 'close') AND status = 'open'", (bot_id,))
            repaired_count += 1
        else:
            logger.info(f"Bot {bot_id} ({name} {direction}): TP is correct ({current_tp:.4f}).")
            
    conn.commit()
    conn.close()
    
    if repaired_count > 0:
        logger.info(f"Successfully repaired {repaired_count} corrupted TP database records.")
    else:
        logger.info("No TP repairs needed.")

if __name__ == "__main__":
    repair_tp_prices()
