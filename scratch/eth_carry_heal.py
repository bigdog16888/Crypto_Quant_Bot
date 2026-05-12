"""
ETH SHORT (bot 10011) CARRY_PENDING heal script.

Situation:
  - physical position: 0.009 ETH SHORT @ 2306.43 (REAL on exchange)
  - trades.cycle_phase = CARRY_PENDING
  - carry entry CQB_10011_CARRY_* filled = 0.009 units
  - No TP or grid orders on exchange (all reset_cleared)
  - Virtual NET: entries(20.754) - exits(20.823) = -0.069 (slight over-exit from cycle history)

Fix:
  1. Set cycle_phase = 'ACTIVE' so the engine's next cycle re-enters normal maintenance
  2. Set current_step = 1 (already correct)
  3. Set total_invested from the carry entry (0.009 * avg_entry ≈ $20.77 — already correct)
  4. Set avg_entry_price to the carry entry price (2307.84 — already correct)
  5. Set open_qty = 0.009 (the physical size)
  6. Clear entry_confirmed = 1 so the bot is treated as "in trade"

The engine will then, on its next cycle:
  - Detect phase=ACTIVE, step=1, no TP order → place a fresh TP
  - The physical position is already there, so no new entry is needed

We do NOT reset or wipe. We do NOT touch bot_orders.
"""
import sqlite3, time

DB = 'crypto_bot.db'
BOT_ID = 10011
BOT_NAME = 'eth'

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Safety check: confirm this is the right bot
row = cur.execute(
    "SELECT id, name, pair, direction, is_active FROM bots WHERE id=?", (BOT_ID,)
).fetchone()
assert row, f"Bot {BOT_ID} not found"
assert row[1] == BOT_NAME, f"Name mismatch: expected {BOT_NAME}, got {row[1]}"
assert row[3] == 'SHORT', f"Direction mismatch: expected SHORT, got {row[3]}"
assert row[4] == 1, "Bot is not active"
print(f"✅ Confirmed bot: {row[1]} (ID={row[0]}) {row[2]} {row[3]}")

# Read current trades state
t = cur.execute(
    "SELECT total_invested, avg_entry_price, current_step, cycle_phase, open_qty, entry_confirmed "
    "FROM trades WHERE bot_id=?", (BOT_ID,)
).fetchone()
print(f"\nBEFORE: invested={t[0]:.4f}  avg_entry={t[1]}  step={t[2]}  phase={t[3]}  open_qty={t[4]}  entry_confirmed={t[5]}")

# Patch: transition CARRY_PENDING → ACTIVE so engine places a fresh TP
cur.execute("""
    UPDATE trades SET
        cycle_phase     = 'ACTIVE',
        open_qty        = 0.009,
        entry_confirmed = 1
    WHERE bot_id = ? AND cycle_phase = 'CARRY_PENDING'
""", (BOT_ID,))

if cur.rowcount == 0:
    print("\n⚠️  No CARRY_PENDING row found — trades table may have already been updated. Aborting without changes.")
    conn.rollback()
    conn.close()
    exit(1)

# Verify
t2 = cur.execute(
    "SELECT total_invested, avg_entry_price, current_step, cycle_phase, open_qty, entry_confirmed "
    "FROM trades WHERE bot_id=?", (BOT_ID,)
).fetchone()
print(f"AFTER:  invested={t2[0]:.4f}  avg_entry={t2[1]}  step={t2[2]}  phase={t2[3]}  open_qty={t2[4]}  entry_confirmed={t2[5]}")

conn.commit()
conn.close()
print(f"\n✅ CARRY_PENDING → ACTIVE patch applied for bot {BOT_ID}.")
print("   Engine will place a fresh TP on its next cycle (~30s).")
print("   Watch engine.log for: [TP-PLACE] or [MAINTAIN] for ETH SHORT.")
