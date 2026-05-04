import sqlite3
conn = sqlite3.connect('crypto_bot.db', timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== BOT 10016 CYCLE 4 ORDERS - WHAT HAPPENED? ===")
cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, 
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=4
    ORDER BY step, order_type, filled_at
""")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

print()
print("=== BOT 10016 CYCLE 5 ORDERS - NEW CYCLE ===")
cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, 
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=5
    ORDER BY step, order_type, filled_at
""")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

print()
print("=== BOT 10022 CYCLE 3 - ALL ORDERS ===")
cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, 
           position_side, filled_at, notes
    FROM bot_orders WHERE bot_id=10022 AND cycle_id=3
    ORDER BY step, order_type, filled_at
""")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

print()
print("=== BOT 10022 - FILLED ORDERS ACROSS ALL CYCLES ===")
cur.execute("""
    SELECT cycle_id, order_type, step, amount, filled_amount, price, status, filled_at
    FROM bot_orders WHERE bot_id=10022 AND filled_amount>0
    ORDER BY filled_at DESC
""")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

print()
print("=== PHYSICAL POSITIONS TABLE ===")
cur.execute("SELECT * FROM active_positions ORDER BY pair, side")
for r in cur.fetchall():
    print(dict(r))

print()
print("=== KEY OBSERVATIONS ===")
print("Bot 10022 (short btc): step=2, invested=389.77, open_qty=0.005")
print("  But NO SHORT BTC appears in active_positions!")
print("  Only LONG BTC (bot_id=10016) appears with 0.054")
print()
print("Bot 10016 (long btc price): step=0, invested=0, open_qty=0 (Scanning)")
print("  But active_positions shows it as LONG 0.054!")
print("  Meaning: it had a LONG that was NOT fully TP'd - position survived after reset!")

print()
print("=== XRP BOT 10017 - IT IS IN TRADE WITH 0 INVESTED ===")
cur.execute("""
    SELECT b.id, b.name, b.status, t.cycle_id, t.current_step, 
           t.total_invested, t.open_qty, t.avg_entry_price, t.entry_confirmed
    FROM bots b JOIN trades t ON t.bot_id=b.id
    WHERE b.id=10017
""")
for r in cur.fetchall():
    print(dict(r))
print("This is a zombie: IN TRADE status but invested=0 and open_qty=0")
print("It shows in active_positions as 0.1 XRP LONG")

print()
print("=== CHECKING RECOMPUTE FOR BOT 10022 ===")
# Check what recompute_invested_from_orders would return for bot 10022 cycle 3
cur.execute("""
    SELECT SUM(filled_amount) as total_qty, 
           SUM(filled_amount * price) as total_cost
    FROM bot_orders 
    WHERE bot_id=10022 AND cycle_id=3 
    AND order_type IN ('entry','grid')
    AND status='filled'
""")
r = cur.fetchone()
if r and r['total_qty']:
    print(f"Bot 10022 recompute: qty={r['total_qty']} cost={r['total_cost']}")
else:
    print("Bot 10022 recompute: NO FILLED ENTRY/GRID ORDERS in cycle 3!")
    print("=> But total_invested=389.77 and open_qty=0.005!")
    print("=> The 'open_qty' accumulator has data that bot_orders cannot back up!")
    print("=> CRITICAL: This is the exact v2.3.2 stale accumulator bug pattern!")

conn.close()
