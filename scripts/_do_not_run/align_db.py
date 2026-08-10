import sqlite3
import time

live_db = "crypto_bot.db"

# Current timestamp in seconds
now_ts = int(time.time())

conn = sqlite3.connect(live_db)
conn.row_factory = sqlite3.Row

print("=== TRADES TABLE BEFORE ALIGNMENT ===")
trades_before = conn.execute("""
    SELECT t.bot_id, b.name, t.current_step, t.open_qty, t.avg_entry_price, t.total_invested, t.cycle_phase, t.position_side, t.wipe_wall_ts
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.total_invested > 0 OR t.current_step > 0
    ORDER BY t.bot_id
""").fetchall()
for t in trades_before:
    print(f"  Bot: {t['bot_id']:<6} | Name: {t['name']:<20} | Step: {t['current_step']:<2} | Qty: {t['open_qty']:<8.4f} | AvgPrice: {t['avg_entry_price']:<10.4f} | Invested: ${t['total_invested']:<8.2f} | Phase: {t['cycle_phase']:<8} | Side: {t['position_side']:<5} | WipeWall: {t['wipe_wall_ts']}")

# Define alignment data based on current exchange positions
# format: bot_id -> (step, qty, avg_price, invested, side, phase)
alignment_data = {
    10007: (1, 0.010, 573.4700, 5.73, 'SHORT', 'ACTIVE'),
    10008: (2, 0.180, 80.3600, 14.46, 'LONG', 'ACTIVE'),
    10016: (5, 0.039, 62992.7000, 2456.71, 'LONG', 'ACTIVE'),
    10021: (3, 0.207, 1759.1700, 364.15, 'LONG', 'ACTIVE'),
    10017: (5, 145.40, 1.11264, 161.78, 'LONG', 'ACTIVE')
}

print(f"\nPerforming alignment. Setting wipe_wall_ts and cycle_start_time to {now_ts}...")

# 1. Update all trades to flat first, calculating individual oldest_fill-based wipe_wall_ts per bot
rows = conn.execute("SELECT bot_id, cycle_id FROM trades").fetchall()
for row in rows:
    bid = row['bot_id']
    cid = row['cycle_id'] or 1
    oldest_fill = conn.execute(
        "SELECT MIN(created_at) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0",
        (bid, cid)
    ).fetchone()
    oldest_ts = oldest_fill[0] if oldest_fill and oldest_fill[0] else now_ts
    wall_ts = min(now_ts, oldest_ts)
    
    conn.execute("""
        UPDATE trades
        SET current_step = 0,
            open_qty = 0.0,
            avg_entry_price = 0.0,
            total_invested = 0.0,
            cycle_phase = 'IDLE',
            entry_confirmed = 0,
            entry_order_id = NULL,
            tp_order_id = NULL,
            bot_position_id = NULL,
            close_type = NULL,
            basket_start_time = 0,
            cycle_start_time = ?,
            wipe_wall_ts = ?
        WHERE bot_id = ?
    """, (now_ts, wall_ts, bid))

# 2. Apply active positions
for bot_id, (step, qty, avg_price, invested, side, phase) in alignment_data.items():
    # Fetch cycle_id
    t_row = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
    cycle_id = t_row['cycle_id'] if t_row else 1

    # Fetch oldest filled order in current cycle
    oldest_fill = conn.execute(
        "SELECT MIN(created_at) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0",
        (bot_id, cycle_id)
    ).fetchone()
    oldest_ts = oldest_fill[0] if oldest_fill and oldest_fill[0] else now_ts
    wall_ts = min(now_ts, oldest_ts)

    conn.execute("""
        UPDATE trades
        SET current_step = ?,
            open_qty = ?,
            avg_entry_price = ?,
            total_invested = ?,
            position_side = ?,
            cycle_phase = ?,
            entry_confirmed = 1,
            basket_start_time = ?,
            cycle_start_time = ?,
            wipe_wall_ts = ?
        WHERE bot_id = ?
    """, (step, qty, avg_price, invested, side, phase, now_ts, now_ts, wall_ts, bot_id))
    
    # Also ensure the bot's status in the bots table is set to 'IN TRADE'
    conn.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))

# Ensure inactive/flat bots are marked as 'Scanning' or 'Stopped'
conn.execute("UPDATE bots SET status = 'Scanning' WHERE id NOT IN (10007, 10008, 10016, 10021, 10017)")

conn.commit()
print("Database alignment complete.")

# 3. Run global sync_trades_from_orders for all active bots to ensure no zeroed intermediate states
print("\n=== RUNNING GLOBAL SYNC FOR ALL ACTIVE BOTS ===")
try:
    from engine.database import sync_trades_from_orders
    active_bots = conn.execute("SELECT id FROM bots WHERE is_active = 1").fetchall()
    for (bid,) in active_bots:
        sync_trades_from_orders(bid)
    print("Global sync completed.")
except Exception as e:
    print(f"Error during global sync: {e}")

print("\n=== TRADES TABLE AFTER ALIGNMENT ===")
trades_after = conn.execute("""
    SELECT t.bot_id, b.name, t.current_step, t.open_qty, t.avg_entry_price, t.total_invested, t.cycle_phase, t.position_side, t.wipe_wall_ts
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.total_invested > 0 OR t.current_step > 0
    ORDER BY t.bot_id
""").fetchall()
for t in trades_after:
    print(f"  Bot: {t['bot_id']:<6} | Name: {t['name']:<20} | Step: {t['current_step']:<2} | Qty: {t['open_qty']:<8.4f} | AvgPrice: {t['avg_entry_price']:<10.4f} | Invested: ${t['total_invested']:<8.2f} | Phase: {t['cycle_phase']:<8} | Side: {t['position_side']:<5} | WipeWall: {t['wipe_wall_ts']}")

conn.close()
