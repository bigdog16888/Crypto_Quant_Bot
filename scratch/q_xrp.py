import sqlite3
conn = sqlite3.connect('crypto_bot.db')
rows = conn.execute("SELECT pair, side, size, entry_price, last_updated FROM active_positions WHERE pair LIKE '%XRP%'").fetchall()
print("active_positions XRP rows:", len(rows))
for r in rows:
    print(r)

# Also check bot_orders for xrp_hedge pending_placement TP
print("\n--- pending_placement TPs ---")
tp_rows = conn.execute("""
    SELECT bo.bot_id, b.name, bo.order_type, bo.status, bo.price, bo.amount, bo.client_order_id, bo.cycle_id, bo.created_at
    FROM bot_orders bo
    JOIN bots b ON b.id = bo.bot_id
    WHERE bo.status = 'pending_placement'
    ORDER BY bo.created_at DESC
    LIMIT 10
""").fetchall()
for r in tp_rows:
    print(r)

# Also check recent cancelled TPs for XRP hedge
print("\n--- recent cancelled/failed bot_orders for xrp_hedge bots ---")
cancel_rows = conn.execute("""
    SELECT bo.bot_id, b.name, bo.order_type, bo.status, bo.price, bo.amount, bo.client_order_id, bo.created_at
    FROM bot_orders bo
    JOIN bots b ON b.id = bo.bot_id
    WHERE b.name LIKE '%xrp%' AND b.name LIKE '%hedge%'
    ORDER BY bo.created_at DESC
    LIMIT 20
""").fetchall()
for r in cancel_rows:
    print(r)

conn.close()
