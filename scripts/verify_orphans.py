import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# BTC/USDC exchange_fills (all)
print('=== exchange_fills BTC/USDC ===')
c.execute("""SELECT id, bot_id, symbol, side, qty, price, source, cycle_id, step, client_order_id, exchange_order_id 
FROM exchange_fills WHERE symbol IN ('BTCUSDC','BTC/USDC:USDC') ORDER BY id""")
rows = c.fetchall()
for r in rows:
    print(r)

print()
print('=== Bot configs for BTC/USDC ===')
c.execute("""SELECT id, pair, direction, status FROM bots 
WHERE pair IN ('BTC/USDC:USDC','BTCUSDC') ORDER BY id""")
for r in c.fetchall():
    print(r)

# Exchange position for BTC/USDC
print()
print('=== Exchange position check ===')
c.execute("""SELECT symbol, SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END) as net_qty
FROM exchange_fills WHERE symbol IN ('BTCUSDC','BTC/USDC:USDC') GROUP BY symbol""")
for r in c.fetchall():
    print(r)

# Test bot fixtures in bots table
print()
print('=== Test bot fixtures (all bots with test-like names) ===')
c.execute("""SELECT id, pair, name, direction, is_active, status, bot_type FROM bots 
WHERE name LIKE '%TEST%' OR name LIKE '%test%' OR bot_type LIKE '%test%' 
ORDER BY id""")
for r in c.fetchall():
    print(r)

# Count of test bots
c.execute("""SELECT COUNT(*) FROM bots WHERE name LIKE '%TEST%' OR name LIKE '%test%' OR bot_type LIKE '%test%'""")
print(f"\nTotal test bots: {c.fetchone()[0]}")

# exchange_fills from test bots
print()
print('=== exchange_fills from test bots ===')
c.execute("""SELECT ef.id, ef.bot_id, b.name, b.pair, ef.symbol, ef.side, ef.qty, ef.price, ef.source
FROM exchange_fills ef
JOIN bots b ON ef.bot_id = b.id
WHERE b.name LIKE '%TEST%' OR b.name LIKE '%test%' OR b.bot_type LIKE '%test%'
ORDER BY ef.id""")
for r in c.fetchall():
    print(r)

conn.close()