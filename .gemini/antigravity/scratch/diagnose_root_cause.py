"""
ROOT CAUSE DIAGNOSTIC SCRIPT - 2026-04-27 Monday
Investigates the fundamental source of the position divergences:
- BTCUSDC NET: System -389.77 vs Exchange +4,209
- SUIUSDC NET: System 0 vs Exchange +27.59  
- SOLUSDC NET: System -9.40 vs Exchange -28.21
"""
import sqlite3

conn = sqlite3.connect('crypto_bot.db', timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

SEP = "=" * 70

print(SEP)
print("SECTION 1: ACTIVE_POSITIONS vs TRADES STATE")
print(SEP)

cur.execute("SELECT * FROM active_positions ORDER BY pair")
active_pos = {(r['pair'], r['side']): dict(r) for r in cur.fetchall()}
for k, v in active_pos.items():
    print(f"  active_pos: {v['pair']} {v['side']} size={v['size']} entry={v['entry_price']:.4f} bot_id={v['bot_id']}")

print()
cur.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status as b_status, b.is_active,
           t.cycle_id, t.total_invested, t.open_qty, t.avg_entry_price,
           t.entry_confirmed, t.cycle_start_time, t.basket_start_time,
           t.position_side, t.wipe_wall_ts, t.current_step
    FROM bots b LEFT JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active=1
    ORDER BY b.pair, b.direction
""")
bots = cur.fetchall()
for r in bots:
    r = dict(r)
    print(f"  bot={r['id']} name={r['name']} pair={r['pair']} dir={r['direction']}")
    print(f"       status={r['b_status']} cycle={r['cycle_id']} step={r['current_step']}")
    print(f"       invested={r['total_invested']} open_qty={r['open_qty']} entry={r['avg_entry_price']}")
    print()

print(SEP)
print("SECTION 2: BOT 10016 (long btc price) - FULL ORDER HISTORY")
print(SEP)

cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, 
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=10016
    ORDER BY cycle_id DESC, filled_at DESC LIMIT 30
""")
for r in cur.fetchall():
    r = dict(r)
    print(f"  cyc={r['cycle_id']} type={r['order_type']} step={r['step']} qty={r['amount']} "
          f"filled={r['filled_amount']} px={r['price']} status={r['status']} "
          f"at={r['filled_at']} notes={r['notes']}")

print()
print(SEP)
print("SECTION 3: BOT 10018 (sui long) - CRITICAL: filled orders cycle 10")
print(SEP)

cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, 
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=10018
    AND status IN ('filled', 'reset_cleared', 'open', 'new')
    ORDER BY cycle_id DESC, filled_at DESC LIMIT 30
""")
for r in cur.fetchall():
    r = dict(r)
    print(f"  cyc={r['cycle_id']} type={r['order_type']} step={r['step']} qty={r['amount']} "
          f"filled={r['filled_amount']} px={r['price']} status={r['status']} "
          f"at={r['filled_at']} notes={r['notes']}")

print()
print(SEP)
print("SECTION 4: BOT 100001 (short sol) - CURRENT CYCLE ORDERS")
print(SEP)

cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status,
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=100001
    AND cycle_id = 5
    ORDER BY step, filled_at
""")
for r in cur.fetchall():
    r = dict(r)
    print(f"  cyc={r['cycle_id']} type={r['order_type']} step={r['step']} qty={r['amount']} "
          f"filled={r['filled_amount']} px={r['price']} status={r['status']} "
          f"at={r['filled_at']} notes={r['notes']}")

print()
print(SEP)
print("SECTION 5: WHAT RECONCILER THINKS ABOUT NET VIRTUAL vs PHYSICAL")
print(SEP)

# Replicate the monitor's logic precisely
cur.execute("""
    SELECT b.pair, b.direction,
           t.total_invested, t.avg_entry_price,
           COALESCE(t.open_qty, 0) as open_qty
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
      AND (t.total_invested > 0 OR t.open_qty > 0)
""")
virt_rows = cur.fetchall()

virtual_qty_by_pair = {}
pair_prices = {}

def normalize(pair_str):
    # BTC/USDC:USDC -> BTCUSDC
    return pair_str.replace('/', '').split(':')[0]

for r in virt_rows:
    r = dict(r)
    invested = float(r['total_invested'] or 0)
    avg_price = float(r['avg_entry_price'] or 0)
    open_qty_v = float(r['open_qty'] or 0)
    pair_key = normalize(r['pair'])
    side_key = str(r['direction']).upper()
    composite_key = (pair_key, side_key)

    if open_qty_v > 0:
        qty_abs = open_qty_v
        ref_price = avg_price if avg_price > 0 else 1.0
    elif invested > 0 and avg_price > 0:
        qty_abs = invested / avg_price
        ref_price = avg_price
    else:
        continue

    if pair_key not in pair_prices:
        pair_prices[pair_key] = ref_price
    virtual_qty_by_pair[composite_key] = virtual_qty_by_pair.get(composite_key, 0.0) + qty_abs

print("Virtual (from monitor logic):")
for k, v in virtual_qty_by_pair.items():
    print(f"  {k}: qty={v:.4f} price={pair_prices.get(k[0], 0):.2f} usd={v*pair_prices.get(k[0], 0):.2f}")

print()
physical_qty_by_pair = {}
for k, ap in active_pos.items():
    pair_key = ap['pair']  # already normalized (e.g. BTCUSDC)
    side_key = 'LONG' if ap['side'].upper() in ('BUY', 'LONG') else 'SHORT'
    composite_key = (pair_key, side_key)
    qty = abs(float(ap['size']))
    price = float(ap['entry_price'])
    if pair_key not in pair_prices:
        pair_prices[pair_key] = price
    physical_qty_by_pair[composite_key] = physical_qty_by_pair.get(composite_key, 0.0) + qty

print("Physical (from active_positions):")
for k, v in physical_qty_by_pair.items():
    print(f"  {k}: qty={v:.4f}")

print()
print("NET COMPARISON:")
all_pairs = set([k[0] for k in virtual_qty_by_pair] + [k[0] for k in physical_qty_by_pair])
for p in sorted(all_pairs):
    v_long = virtual_qty_by_pair.get((p, 'LONG'), 0.0)
    v_short = virtual_qty_by_pair.get((p, 'SHORT'), 0.0)
    ph_long = physical_qty_by_pair.get((p, 'LONG'), 0.0)
    ph_short = physical_qty_by_pair.get((p, 'SHORT'), 0.0)
    v_net = v_long - v_short
    ph_net = ph_long - ph_short
    ref_p = pair_prices.get(p, 0.0)
    diff_qty = ph_net - v_net
    diff_usd = abs(diff_qty) * ref_p
    v_usd = v_net * ref_p
    ph_usd = ph_net * ref_p
    if diff_usd > 0.01:
        flag = " <-- MISMATCH"
    else:
        flag = ""
    print(f"  {p}: virt_net={v_net:+.4f} phys_net={ph_net:+.4f} diff={diff_qty:+.4f} "
          f"v_usd={v_usd:+.2f} ph_usd={ph_usd:+.2f} diff_usd={diff_usd:.2f}{flag}")

print()
print(SEP)
print("SECTION 6: KEY DIAGNOSTICS FOR ROOT CAUSE")
print(SEP)

# Check what happened to bot 10016 in cycle 4 vs cycle 5
print("\n--- Bot 10016 cycle 4 orders (THE CRITICAL CYCLE) ---")
cur.execute("""
    SELECT order_type, step, amount, filled_amount, price, status, filled_at, notes
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=4
    ORDER BY step, filled_at
""")
for r in cur.fetchall():
    r = dict(r)
    print(f"  type={r['order_type']} step={r['step']} qty={r['amount']} "
          f"filled={r['filled_amount']} px={r['price']} status={r['status']} "
          f"at={r['filled_at']} notes={r['notes']}")

print("\n--- Bot 10016 cycle 5 orders (THE NEW CYCLE) ---")
cur.execute("""
    SELECT order_type, step, amount, filled_amount, price, status, filled_at, notes
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=5
    ORDER BY step, filled_at
""")
for r in cur.fetchall():
    r = dict(r)
    print(f"  type={r['order_type']} step={r['step']} qty={r['amount']} "
          f"filled={r['filled_amount']} px={r['price']} status={r['status']} "
          f"at={r['filled_at']} notes={r['notes']}")

print("\n--- Checking: was there a REAL TP fill for bot 10016? ---")
cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, filled_at
    FROM bot_orders WHERE bot_id=10016 AND order_type='tp' AND filled_amount > 0
    ORDER BY filled_at DESC
""")
tp_fills = cur.fetchall()
if tp_fills:
    for r in tp_fills:
        print(f"  REAL TP FILL: {dict(r)}")
else:
    print("  NO REAL TP FILLS FOUND for bot 10016!")
    print("  => Bot was reset WITHOUT a real TP fill on the exchange!")
    print("  => This is the smoking gun: a phantom reset created the Scanning state")
    print("     while the LONG position remained open on the exchange.")

print()
print("--- Checking: bot 10022 short btc vs physical ---")
print("Bot 10022 (short btc): open_qty=0.005, invested=389.77, step=2")
cur.execute("""
    SELECT order_type, step, amount, filled_amount, price, status, filled_at
    FROM bot_orders WHERE bot_id=10022 AND cycle_id=3 AND status='filled'
    ORDER BY filled_at
""")
print("FILLED orders for bot 10022 cycle 3:")
for r in cur.fetchall():
    print(f"  {dict(r)}")

conn.close()
print()
print(SEP)
print("DIAGNOSTIC COMPLETE")
print(SEP)
