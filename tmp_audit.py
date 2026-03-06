from engine.database import get_connection
conn = get_connection()
c = conn.cursor()
c.execute("""SELECT b.id, b.name, b.pair, b.direction, 
    COALESCE(t.total_invested,0), 
    COALESCE(t.current_step,0),
    COALESCE(t.entry_confirmed,0),
    COALESCE(t.cycle_id, 1)
    FROM bots b LEFT JOIN trades t ON b.id=t.bot_id 
    WHERE b.is_active=1 ORDER BY b.pair, b.direction""")
rows = c.fetchall()
print(f"{'ID':<5} {'NAME':<26} {'PAIR':<14} {'DIR':<6} {'INVESTED':>10} {'STEP':>5} {'CONF':<5} {'CID'}")
print('-'*90)
for r in rows:
    bot_id, name, pair, direction, inv, step, conf, cid = r
    flag = " <-- GHOST?" if inv > 10 else ""
    print(f"{bot_id:<5} {name[:25]:<26} {pair:<14} {direction:<6} {inv:>10.2f} {step:>5} {'YES' if conf else 'NO':<5} {cid}{flag}")

# Also check for bot_orders to see recent fills
print("\n--- Recent bot_orders (last 20 filled) ---")
c.execute("""SELECT bo.bot_id, b.name, b.pair, bo.order_type, bo.status, bo.price, bo.amount, bo.created_at
    FROM bot_orders bo JOIN bots b ON bo.bot_id=b.bot_id
    WHERE bo.status IN ('filled','closed') 
    ORDER BY bo.created_at DESC LIMIT 20""")
fills = c.fetchall()
if not fills:
    print("  No recent fills found in bot_orders")
else:
    for f in fills:
        print(f"  Bot {f[0]} ({f[1]}) {f[2]} | {f[3]} | {f[4]} | ${f[5]:.4f} x {f[6]:.4f}")
conn.close()
