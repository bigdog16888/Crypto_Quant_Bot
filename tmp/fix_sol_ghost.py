"""
Correct the SOL bot 10008 trades row to match the ground-truth from bot_orders.
This uses recompute_invested_from_orders (which was just patched to subtract adoption_reduce).
"""
import sys
sys.path.insert(0, '.')
from engine.database import recompute_invested_from_orders, get_connection
import time

bot_id = 10008
total_invested, avg_entry, step = recompute_invested_from_orders(bot_id)
print(f"recompute_invested_from_orders result:")
print(f"  total_invested: {total_invested:.4f}")
print(f"  avg_entry:      {avg_entry:.4f}")
print(f"  step:           {step}")

if total_invested > 0 and avg_entry > 0:
    conn = get_connection()
    c = conn.cursor()
    old = c.execute("SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    print(f"\nCurrent DB: invested={old[0]:.2f}, avg={old[1]:.4f}, step={old[2]}")
    
    c.execute("""
        UPDATE trades
        SET total_invested = ?, avg_entry_price = ?, current_step = ?
        WHERE bot_id = ?
    """, (total_invested, avg_entry, step, bot_id))
    conn.commit()
    conn.close()
    print(f"✅ Updated trades for bot {bot_id}: invested=${total_invested:.2f} @ {avg_entry:.4f}, step={step}")
else:
    print(f"⚠️ recompute returned zeros — manual check needed. No changes made.")
