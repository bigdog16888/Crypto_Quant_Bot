"""
The SOL DB is correct ($1513 = 16.65 SOL @ $90.90 exchange).
The per-step orders audit was misleading due to duplicated adoption_reduce rows.
Clean up the corrupted duplicate adoption_reduce rows to fix the ledger consistency.
Mark ONLY the obviously-duplicate reduce rows as 'auto_closed'.
"""
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

# Show all adoption_reduce rows for bot 10008 cycle 46
print("=== adoption_reduce rows for SOL 10008 cycle 46 ===")
rows = conn.execute("""
    SELECT id, step, filled_amount, price, status, client_order_id, created_at
    FROM bot_orders
    WHERE bot_id=10008 AND cycle_id=46
      AND order_type='adoption_reduce'
    ORDER BY created_at ASC
""").fetchall()
for r in rows:
    print(f"  id={r[0]} step={r[1]} qty={r[2]:.4f} price={r[3]} status={r[4]} cid={r[5]} ts={r[6]}")

print()
# Verify exchange ground truth:
# 16.65 contracts @ $90.9056 = $1512.98 ≈ DB $1513.58 ✅
# This means the DB is right. The adoption_reduce duplicates don't affect the trades table
# (they're already stored but ignored by the main loop — only recompute uses them).
# Since recompute now subtracts them, it would give a wrong answer.
# Solution: mark the clearly-fabricated duplicate reduces as 'reset_cleared'.

# The step-9 reduces of 20.02 SOL each are clearly duplicates/errors (there's no 20 SOL partial).
# The actual physical position is 16.65 SOL, so we can't have reduced >16.65 SOL total.
# Flag rows with filled_amount=20.02 (impossible size given physical) as auto_closed.
bad_rows = conn.execute("""
    SELECT id, filled_amount FROM bot_orders
    WHERE bot_id=10008 AND cycle_id=46
      AND order_type='adoption_reduce'
      AND filled_amount >= 20.0
""").fetchall()

print(f"Rows to mark auto_closed (impossible qty >= 20 SOL): {len(bad_rows)}")
for r in bad_rows:
    print(f"  id={r[0]} filled_amount={r[1]}")

if bad_rows:
    ids = [r[0] for r in bad_rows]
    conn.execute(f"UPDATE bot_orders SET status='auto_closed' WHERE id IN ({','.join(str(x) for x in ids)})")
    conn.commit()
    print(f"✅ Marked {len(ids)} corrupted reduction rows as auto_closed")

conn.close()
