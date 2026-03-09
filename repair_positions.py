"""
Patch LINK and XRP virtual positions using data from active_positions (WS-populated).
No exchange API call needed — WS already has the truth.
"""
import sqlite3, time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Read physical positions from WS-populated active_positions table
c.execute("SELECT pair, side, size, entry_price FROM active_positions")
phys = {r[0]: {'side': r[1], 'qty': r[2], 'avg': r[3]} for r in c.fetchall()}
print("Physical positions from WS:")
for k, v in phys.items():
    notional = v['qty'] * v['avg']
    print(f"  {k}: {v['side']} qty={v['qty']:.4f} avg={v['avg']:.4f} notional=${notional:.2f}")

# Bots to repair: bot_id, pair key in active_positions
to_repair = [
    (10020, 'LINK/USDC'),
    (10017, 'XRP/USDC'),
]

for bot_id, pair_key in to_repair:
    p = phys.get(pair_key)
    if not p:
        print(f"\n{pair_key}: No physical position found — skipping.")
        continue

    phys_notional = p['qty'] * p['avg']
    phys_avg = p['avg']

    c.execute("SELECT total_invested, avg_entry_price, current_step, basket_start_time FROM trades WHERE bot_id=?", (bot_id,))
    row = c.fetchone()
    if not row:
        print(f"\nBot {bot_id}: No trades row — skipping.")
        continue

    db_invested, db_avg, db_step, db_bst = row
    print(f"\nBot {bot_id} ({pair_key}):")
    print(f"  DB:    invested=${db_invested:.2f}, avg={db_avg:.4f}, step={db_step}")
    print(f"  Phys:  invested=${phys_notional:.2f}, avg={phys_avg:.4f}")

    new_bst = db_bst if (db_bst and db_bst > 0) else int(time.time())
    c.execute(
        "UPDATE trades SET total_invested=?, avg_entry_price=?, basket_start_time=? WHERE bot_id=?",
        (phys_notional, phys_avg, new_bst, bot_id)
    )
    print(f"  ✅ Patched to invested=${phys_notional:.2f}, avg={phys_avg:.4f}, BST={new_bst}")

conn.commit()
conn.close()
print("\n✅ Done. Reconciler will validate on next cycle.")
