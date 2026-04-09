import sqlite3, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Get trades for problem symbols
c.execute("""SELECT b.id, b.name, b.pair, b.direction, b.is_active,
    t.total_invested, t.avg_entry_price, t.current_step, t.entry_confirmed, t.cycle_id
    FROM bots b LEFT JOIN trades t ON b.id=t.bot_id
    WHERE b.pair IN ('LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC','BTCUSDC')
    ORDER BY b.pair, b.id""")
print('=== BOTS+TRADES ===')
for r in c.fetchall():
    print(r)

# Check active_positions
c.execute('SELECT * FROM active_positions')
print()
print('=== ALL ACTIVE POSITIONS ===')
for r in c.fetchall():
    print(r)

# Check what BTC/XRP orphan looks like in bot_orders 
c.execute("""SELECT bot_id, order_type, status, price, amount, filled_amount, client_order_id, created_at
    FROM bot_orders WHERE bot_id IN (
        SELECT id FROM bots WHERE pair='BTCUSDC'
    ) ORDER BY created_at DESC LIMIT 10""")
print()
print('=== BTC RECENT ORDERS IN DB ===')
for r in c.fetchall():
    print(r)

conn.close()
