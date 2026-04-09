import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== ALL ACTIVE BOTS CURRENT STATE ===")
q.execute("""
    SELECT b.id, b.name, b.direction, b.pair,
           t.total_invested, t.avg_entry_price, t.current_step, 
           t.entry_confirmed, t.cycle_id,
           ap.size as phys_qty, ap.bot_id as ap_bot_id
    FROM bots b
    LEFT JOIN trades t ON b.id=t.bot_id
    LEFT JOIN active_positions ap ON ap.bot_id=b.id
    WHERE b.is_active=1
    ORDER BY t.total_invested DESC, b.id
""")
for r in q.fetchall():
    bid, name, direction, pair, ti, avg, step, confirmed, cyc, phys, ap_bid = r
    ti = ti or 0; avg = avg or 0; step = step or 0
    vqty = ti/avg if avg else 0
    sys_str = f"${ti:.2f} step={step} conf={confirmed}"
    phys_str = f"phys={phys}" if phys else "phys=None"
    print(f"  [{bid}] {name} ({direction}): {sys_str} | {phys_str} | ap_bot_id={ap_bid}")

print()
print("=== active_positions bot_id=0 (orphaned again?) ===")
q.execute("SELECT pair, side, size, bot_id FROM active_positions WHERE bot_id=0 OR bot_id IS NULL")
orphans = q.fetchall()
for r in orphans: print(f"  ORPHAN: {r}")
if not orphans: print("  None — good!")

print()
print("=== BTC/ETH bot_orders recent cycle (possible ghost reset) ===")
for bid, name in [(10016, 'long_btc'), (10011, 'eth'), (100002, 'short_eth'), (10021, 'long_eth')]:
    q.execute("""
        SELECT order_type, status, filled_amount, cycle_id, client_order_id
        FROM bot_orders WHERE bot_id=? AND cycle_id=(SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id=?)
        AND status NOT IN ('cancelled','canceled') ORDER BY created_at DESC LIMIT 5
    """, (bid, bid))
    rows = q.fetchall()
    if rows:
        print(f"\n  Bot {bid} ({name}) latest cycle:")
        for r in rows:
            print(f"    {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} [{r[4][:50]}]")

c.close()
