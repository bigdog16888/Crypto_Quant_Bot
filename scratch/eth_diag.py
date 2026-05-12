import sqlite3, time

db = 'crypto_bot.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# Find the eth SHORT bot
bot = conn.execute(
    "SELECT id, name, pair, direction, status FROM bots "
    "WHERE LOWER(name)='eth' AND direction='SHORT' AND is_active=1"
).fetchone()
if not bot:
    # fallback: any ETH SHORT active bot
    bot = conn.execute(
        "SELECT id, name, pair, direction, status FROM bots "
        "WHERE pair LIKE '%ETH%' AND direction='SHORT' AND is_active=1"
    ).fetchone()

if not bot:
    print("Bot not found")
    exit()

bid = bot['id']
print(f"=== BOT: {bot['name']} (ID={bid}) pair={bot['pair']} dir={bot['direction']} status={bot['status']}")
print()

# Trades state
t = conn.execute(
    "SELECT total_invested, avg_entry_price, current_step, cycle_phase, open_qty, "
    "basket_start_time, cycle_start_time, wipe_wall_ts FROM trades WHERE bot_id=?", (bid,)
).fetchone()
if t:
    bkt = t['basket_start_time'] or 0
    ww  = t['wipe_wall_ts'] or 0
    print(f"TRADES: invested=${t['total_invested']:.4f}  avg_entry={t['avg_entry_price']}  step={t['current_step']}  phase={t['cycle_phase']}  open_qty={t['open_qty']}")
    print(f"        basket_age={(time.time()-bkt)/60:.0f}m  wipe_wall_ts={ww}")
print()

# All bot_orders (last 25)
print("BOT_ORDERS (last 25, by created_at desc):")
rows = conn.execute(
    "SELECT order_type, client_order_id, filled_amount, amount, status, step, price, created_at, notes "
    "FROM bot_orders WHERE bot_id=? ORDER BY created_at DESC LIMIT 25", (bid,)
).fetchall()
for r in rows:
    age_m = (time.time() - (r['created_at'] or 0)) / 60
    cid   = (r['client_order_id'] or '')[:45]
    notes = (r['notes'] or '')[:60]
    print(f"  [{r['order_type']:16s}] {cid:45s} filled={r['filled_amount']}  status={r['status']:12s}  step={r['step']}  price={r['price']}  age={age_m:.0f}m  notes={notes}")

print()

# Physical position on exchange snapshot
print("PHYSICAL POSITIONS (active_positions for ETH):")
phys = conn.execute(
    "SELECT pair, side, size, entry_price, last_checked FROM active_positions WHERE pair LIKE '%ETH%'"
).fetchall()
for p in phys:
    print(f"  {p['pair']}  {p['side']}  size={p['size']}  entry={p['entry_price']}  last_checked={p['last_checked']}")
if not phys:
    print("  (none — exchange shows no ETH position in snapshot)")

print()

# get_pair_virtual_net equivalent
print("VIRTUAL NET (from bot_orders filled entries minus exits):")
entry_types = ('entry','grid','adoption','adoption_add','carry')
exit_types  = ('tp','close','sl','dust_close','adoption_reduce','virtual_netting')

entry_qty = conn.execute(
    "SELECT COALESCE(SUM(filled_amount),0) FROM bot_orders "
    "WHERE bot_id=? AND filled_amount>0 AND order_type IN (?,?,?,?,?)",
    (bid, *entry_types)
).fetchone()[0] or 0.0

exit_qty = conn.execute(
    "SELECT COALESCE(SUM(filled_amount),0) FROM bot_orders "
    "WHERE bot_id=? AND filled_amount>0 AND order_type IN (?,?,?,?,?,?)",
    (bid, *exit_types)
).fetchone()[0] or 0.0

drift_rows = conn.execute(
    "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM bot_orders WHERE bot_id=? AND order_type='drift_note'",
    (bid,)
).fetchone()

print(f"  Entries summed : {entry_qty:.6f}")
print(f"  Exits summed   : {exit_qty:.6f}")
print(f"  NET open qty   : {entry_qty - exit_qty:.6f}")
print(f"  drift_note rows: {drift_rows[0]} (total noted qty={drift_rows[1]:.6f})")

conn.close()
