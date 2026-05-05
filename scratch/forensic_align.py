import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FORENSIC-ALIGN")

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'

def align_bot(bot_id, pair):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    try:
        # 1. Get current state
        row = cur.execute("SELECT cycle_id, open_qty, position_side FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
        if not row:
            logger.error(f"Bot {bot_id} not found.")
            return
        
        cycle_id, open_qty, side = row
        
        if open_qty <= 0:
            logger.info(f"Bot {bot_id} ({pair}) already at 0. Skipping.")
            return

        logger.info(f"Aligning Bot {bot_id} ({pair}): Virtual {open_qty} -> Physical 0.00")

        # 2. Add Virtual Netting Entry
        ts = int(time.time() * 1000)
        cid = f"CQB_{bot_id}_FORENSIC_NET_{ts}"
        
        cur.execute("""
            INSERT INTO bot_orders (
                bot_id, cycle_id, order_type, price, amount, filled_amount, status, client_order_id, position_side, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bot_id, cycle_id, 'virtual_netting', 0.0, open_qty, open_qty, 'filled', cid, side, 'Forensic alignment with exchange physical truth (0.00)'
        ))

        # 3. Update trades table
        cur.execute("""
            UPDATE trades 
            SET total_invested = 0.0, 
                avg_entry_price = 0.0, 
                open_qty = 0.0, 
                hedge_qty = 0.0,
                current_step = 0,
                cycle_phase = 'IDLE'
            WHERE bot_id = ?
        """, (bot_id,))
        
        # 4. Release Manual Gate
        cur.execute("UPDATE bots SET status = 'IDLE' WHERE id = ?", (bot_id,))

        conn.commit()
        logger.info(f"✅ Bot {bot_id} aligned to 0.00 parity.")

    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Failed to align bot {bot_id}: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    # Align BTC, SUI Long, SUI Short, XRP Long
    targets = [
        (10022, 'BTC/USDC:USDC'),
        (10018, 'SUI/USDC:USDC'),
        (100000, 'SUI/USDC:USDC'),
        (10017, 'XRP/USDC:USDC')
    ]
    for b_id, pair in targets:
        align_bot(b_id, pair)
