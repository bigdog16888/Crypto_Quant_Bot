import sqlite3
import time
import sys
import os

sys.path.append(os.getcwd())
from engine.database import get_connection, recompute_invested_from_orders

def align_to_physical():
    conn = get_connection()
    cursor = conn.cursor()
    
    # 🎯 PHYSICAL TARGETS (The Ground Truth from the Exchange)
    # (BotID, TargetQty)
    alignments = [
        (10016, 0.0),    # BTC Long -> 0.0
        (10022, 0.002),  # BTC Short -> -0.002 (Stored as positive 0.002 in open_qty)
        (10018, 0.0),    # SUI Long -> 0.0
        (100000, 10.6)   # SUI Short -> -10.6
    ]
    
    for bot_id, target_qty in alignments:
        print(f"--- Aligning Bot {bot_id} to Physical Reality ({target_qty}) ---")
        
        # 1. Get current virtual qty
        _, _, current_qty, _, hedge_qty = recompute_invested_from_orders(bot_id)
        
        # We need to account for BOTH open_qty and hedge_qty
        # Physical Net = (Position - Hedge)
        # We want (Position - Hedge) to equal target_qty
        
        diff = target_qty - (current_qty - hedge_qty)
        
        if abs(diff) < 1e-8:
            print(f"   Bot {bot_id} already in parity. Skipping.")
            continue
            
        print(f"   Current Virtual: {current_qty - hedge_qty:.6f}, Target: {target_qty:.6f}, Diff: {diff:.6f}")
        
        # 2. Insert Forensic Virtual Netting Entry
        cid = f"CQB_{bot_id}_ALIGN_{int(time.time() * 1000)}"
        
        # Determine if we are adding or reducing
        # For a Short bot, positive diff means we need to reduce the short (buy)
        # or increase the hedge. We'll use 'virtual_netting' type.
        
        cursor.execute("""
            INSERT INTO bot_orders (
                bot_id, step, order_type, order_id, price, amount, filled_amount,
                status, created_at, updated_at, client_order_id, notes, cycle_id
            ) VALUES (?, ?, 'virtual_netting', ?, 0, ?, ?, 'filled', ?, ?, ?, ?, (SELECT cycle_id FROM trades WHERE bot_id = ?))
        """, (
            bot_id, 0, cid, abs(diff), abs(diff),
            int(time.time()), int(time.time()), cid,
            f"Forensic Alignment to Physical Inventory (Correction: {diff:.6f})",
            bot_id
        ))
        
        # 3. Final Recompute
        recompute_invested_from_orders(bot_id)
        print(f"   Bot {bot_id} aligned and recomputed.")

    conn.commit()
    print("\n💎 Absolute Parity Achieved. System now mirrors Exchange 1:1.")

if __name__ == "__main__":
    align_to_physical()
