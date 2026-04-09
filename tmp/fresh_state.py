import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("=== ALL BOTS (checking schema) ===")
c.execute("SELECT * FROM bots LIMIT 3")
print("bots schema:", [d[0] for d in c.description])
c.execute("SELECT id, name, pair, direction, is_active, status FROM bots WHERE pair IN ('LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC','BTCUSDC') ORDER BY pair")
for r in c.fetchall():
    print(r)

print()
print("=== TRADES FOR THOSE BOT IDs ===")
c.execute("SELECT * FROM trades LIMIT 1")
print("trades schema:", [d[0] for d in c.description])
c.execute("""
    SELECT t.bot_id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price, 
           t.current_step, t.entry_confirmed, t.cycle_id
    FROM bots b JOIN trades t ON b.id = t.bot_id
    WHERE b.pair IN ('LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC','BTCUSDC')
    ORDER BY b.pair
""")
for r in c.fetchall():
    print(r)

print()
print("=== ACTIVE POSITIONS (current state) ===")
c.execute("SELECT * FROM active_positions")
for r in c.fetchall():
    print(r)

print()
print("=== LAST 10 BOT_ORDERS FOR LINK BOT 10020 ===")
c.execute("""SELECT id, step, order_type, price, amount, filled_amount, status, client_order_id,
    datetime(created_at,'unixepoch','localtime')
    FROM bot_orders WHERE bot_id=10020 ORDER BY created_at DESC LIMIT 10""")
for r in c.fetchall():
    print(r)

print()
print("=== LAST 10 BOT_ORDERS FOR SUI BOT 10018 ===")
c.execute("""SELECT id, step, order_type, price, amount, filled_amount, status, client_order_id,
    datetime(created_at,'unixepoch','localtime')
    FROM bot_orders WHERE bot_id=10018 ORDER BY created_at DESC LIMIT 10""")
for r in c.fetchall():
    print(r)

print()
print("=== LAST 10 BOT_ORDERS FOR SOL (bot 10008) ===")
c.execute("""SELECT id, step, order_type, price, amount, filled_amount, status, client_order_id,
    datetime(created_at,'unixepoch','localtime')
    FROM bot_orders WHERE bot_id=10008 ORDER BY created_at DESC LIMIT 10""")
for r in c.fetchall():
    print(r)

print()    
print("=== LAST 10 BOT_ORDERS FOR BTC bots ===")
c.execute("""
    SELECT bo.bot_id, bo.step, bo.order_type, bo.price, bo.amount, bo.filled_amount, bo.status, bo.client_order_id,
        datetime(bo.created_at,'unixepoch','localtime')
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair='BTCUSDC'
    ORDER BY bo.created_at DESC LIMIT 15
""")
for r in c.fetchall():
    print(r)

conn.close()
