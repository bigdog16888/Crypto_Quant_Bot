import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BTC-RECOVERY-V2")

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
bot_id = 10022

def restore_bot():
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    try:
        # 1. Identify the previous cycle
        cur.execute("SELECT cycle_id, name FROM trades t JOIN bots b ON t.bot_id=b.id WHERE bot_id=?", (bot_id,))
        row = cur.fetchone()
        if not row:
            logger.error("Bot not found.")
            return
        
        current_cycle = row[0]
        bot_name = row[1]
        
        # If we already restored and it failed, we might be back at cycle 11 or still 12
        if current_cycle == 12:
            target_cycle = 11
        else:
            target_cycle = current_cycle # Already at 11 from previous failed attempt
        
        logger.info(f"Aggressive Restoration for {bot_name} (Cycle {target_cycle})")

        # 2. DELETE the "Wipe Wall" markers AND fake wipe residue
        cur.execute("""
            DELETE FROM bot_orders 
            WHERE bot_id = ? AND cycle_id = ? 
              AND (
                (status IN ('reset_cleared', 'auto_closed') AND filled_amount = 0)
                OR (order_type IN ('adoption_reduce', 'virtual_netting') AND client_order_id LIKE 'CQB_%_ADOPT_%')
              )
        """, (bot_id, target_cycle))
        deleted_rows = cur.rowcount
        
        # 3. Restore all legitimate fills to 'filled' status
        cur.execute("""
            UPDATE bot_orders 
            SET status = 'filled' 
            WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0
        """, (bot_id, target_cycle))
        restored_fills = cur.rowcount
        
        # COMMIT NOW so recompute_invested_from_orders sees the changes
        conn.commit()
        logger.info(f"Purged {deleted_rows} residue rows and restored {restored_fills} fills. Committed.")

        # 4. Delete the Cycle 12 leftovers if they exist
        cur.execute("DELETE FROM bot_orders WHERE bot_id = ? AND cycle_id = 12", (bot_id,))
        conn.commit()
        
        # 5. Recompute mathematical truth from the un-archived Cycle 11 ledger
        from engine.database import recompute_invested_from_orders
        cost, avg, qty, step, h_qty = recompute_invested_from_orders(bot_id, cycle_id=target_cycle)
        
        logger.info(f"FINAL RECOMPUTED STATE: Step={step}, Cost=${cost:.2f}, Avg=${avg:.2f}, Qty={qty:.6f}, Hedge={h_qty:.6f}")

        if qty < 0.0001:
            logger.error("Recomputation still returned ZERO. Restoration failed.")
            return

        # 6. Update trades table to Cycle 11 reality
        cur.execute("""
            UPDATE trades 
            SET cycle_id = ?, 
                current_step = ?, 
                total_invested = ?, 
                avg_entry_price = ?, 
                open_qty = ?, 
                hedge_qty = ?,
                cycle_phase = 'ACTIVE'
            WHERE bot_id = ?
        """, (target_cycle, step, cost, avg, qty, h_qty, bot_id))
        
        # 7. Set status back to 'IN TRADE'
        cur.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))

        conn.commit()
        logger.info("✅ SUCCESS: BTC bot restored to Cycle 11 Step 10 state.")

    except Exception as e:
        conn.rollback()
        logger.error(f"❌ FAILED to restore BTC bot: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    restore_bot()
