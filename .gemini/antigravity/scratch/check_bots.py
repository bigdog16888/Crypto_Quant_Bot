import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# What's the current live state of ALL bots
cur.execute('''SELECT b.id, b.name, b.pair, b.direction, b.status,
    t.total_invested, t.avg_entry_price, t.open_qty, t.current_step, t.cycle_id, t.cycle_phase, t.tp_order_id
FROM bots b LEFT JOIN trades t ON b.id=t.bot_id WHERE b.is_active=1 ORDER BY b.id''')
rows = cur.fetchall()
for r in rows:
    d = dict(r)
    inv = d['total_invested'] or 0
    step = d['current_step']
    oq = d['open_qty']
    phase = d['cycle_phase']
    tp = d['tp_order_id']
    print(f"Bot {d['id']:5d} | {str(d['name']):20s} | {str(d['direction']):5s} | {str(d['status']):12s} | inv={inv:8.2f} | step={step} | open_qty={oq} | phase={phase} | tp={tp}")

print("\n=== RECENT TP CANCEL STORM ===")
# Find any bot that has >5 cancelled TPs in last 5 minutes
cur.execute('''SELECT bot_id, COUNT(*) as cnt, MAX(created_at) as last
FROM bot_orders WHERE order_type='tp' AND status IN ('cancelled','canceled')
AND created_at > (strftime('%s','now') - 600)
GROUP BY bot_id HAVING cnt > 3 ORDER BY cnt DESC''')
for r in cur.fetchall():
    print(f"  Bot {r['bot_id']}: {r['cnt']} cancelled TPs in last 10min")

print("\n=== OPEN_QTY vs RECOMPUTED MISMATCH ===")
cur.execute('''SELECT t.bot_id, b.name, t.open_qty, t.total_invested, t.avg_entry_price, t.cycle_id
FROM trades t JOIN bots b ON t.bot_id=b.id WHERE t.total_invested > 0 OR t.open_qty > 0''')
for r in cur.fetchall():
    d = dict(r)
    # recompute net qty from orders
    sub = conn.cursor()
    sub.execute('''SELECT 
        COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END), 0) as buys,
        COALESCE(SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce') THEN filled_amount ELSE 0 END), 0) as sells
    FROM bot_orders WHERE bot_id=? AND cycle_id=? AND filled_amount>0 AND status NOT IN ('reset_cleared','auto_closed')''',
    (d['bot_id'], d['cycle_id']))
    sr = sub.fetchone()
    net_from_orders = round((sr[0] or 0) - (sr[1] or 0), 8)
    avg = d['avg_entry_price'] or 0
    invested_from_qty = net_from_orders * avg if avg > 0 else 0
    mismatch = abs((d['open_qty'] or 0) - net_from_orders)
    if mismatch > 0.001:
        print(f"  MISMATCH Bot {d['bot_id']} ({d['name']}): open_qty={d['open_qty']} vs orders_net={net_from_orders:.6f} | diff={mismatch:.6f}")

conn.close()
