"""
Emergency repair script: reset corrupted current_step values in trades table.

The PASS 3 reconciler bug computed step = int(phys_qty / base_size) which gives
the total fill count, not the grid index. E.g. BTC: 0.035 / 0.00025 = 140 (wrong!).

This script:
1. Shows all bots with step > realistic threshold
2. Resets them to MAX(bo.step) from bot_orders (CID-proven step)
3. Falls back to step=1 if no bot_orders exist (position adopted via PASS 3,
   meaning we have a physical position but no fill history = single entry)
"""
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

SUSPICIOUS_STEP_THRESHOLD = 10  # Any step > this is likely corrupted

conn = get_connection()
cursor = conn.cursor()

# Show current trades state
rows = cursor.execute("""
    SELECT b.id, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price
    FROM trades t
    JOIN bots b ON b.id = t.bot_id
    ORDER BY t.current_step DESC
""").fetchall()

print("=== Current Trades State ===")
print(f"{'BotID':<6} {'Symbol':<12} {'Dir':<6} {'Step':<8} {'Invested':<12} {'AvgEntry':<12}")
for r in rows:
    flag = " ← CORRUPTED?" if r[3] > SUSPICIOUS_STEP_THRESHOLD else ""
    print(f"{r[0]:<6} {r[1]:<12} {r[2]:<6} {r[3]:<8} {r[4]:<12.2f} {r[5]:<12.4f}{flag}")

print()

# For each bot with suspiciously high step, determine the correct value
repaired = 0
for r in rows:
    bot_id, symbol, direction, current_step = r[0], r[1], r[2], r[3]
    if current_step <= SUSPICIOUS_STEP_THRESHOLD:
        continue

    # Get MAX proven step from bot_orders (CID-anchored)
    cycle_row = conn.execute(
        "SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id=?", (bot_id,)
    ).fetchone()
    cycle_id = cycle_row[0] if cycle_row else 1

    proven_step_row = conn.execute("""
        SELECT COALESCE(MAX(step), 0)
        FROM bot_orders
        WHERE bot_id=? AND cycle_id=?
          AND filled_amount > 0
          AND order_type IN ('entry', 'grid')
          AND client_order_id LIKE 'CQB_%'
    """, (bot_id, cycle_id)).fetchone()

    proven_step = int(proven_step_row[0]) if proven_step_row else 0

    # Physical position exists (step>0 set by PASS3 means entry happened)
    # If no proven fills in bot_orders yet, minimum step=1 (we have a position)
    new_step = max(proven_step, 1)

    print(f"Bot {bot_id} ({symbol} {direction}): step {current_step} → {new_step} "
          f"(proven_max={proven_step})")

    cursor.execute(
        "UPDATE trades SET current_step=? WHERE bot_id=?",
        (new_step, bot_id)
    )
    repaired += 1

conn.commit()
conn.close()

print(f"\n✅ Repaired {repaired} bot(s) with corrupted step values.")
if repaired == 0:
    print("   No corrupted steps found (threshold > {SUSPICIOUS_STEP_THRESHOLD}).")
print("Restart the bot system for the changes to take full effect.")
