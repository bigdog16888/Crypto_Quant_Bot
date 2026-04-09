import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== ETH - ALL ACTIVE BOT_ORDERS (not reset_cleared/auto_closed) ===")
q.execute("""
    SELECT b.name, b.direction, bo.order_type, bo.status, bo.filled_amount, bo.cycle_id
    FROM bots b JOIN bot_orders bo ON b.id=bo.bot_id
    WHERE b.pair LIKE '%ETH%' AND b.is_active=1 
    AND bo.status NOT IN ('reset_cleared','auto_closed','cancelled','canceled')
    AND bo.filled_amount > 0
    ORDER BY b.id, bo.created_at DESC
""")
for r in q.fetchall(): print(' ', r)

print()
print("=== ETH - TRADES ===")
q.execute("SELECT b.id,b.name,b.direction,t.total_invested,t.avg_entry_price,t.current_step,t.cycle_id,t.entry_confirmed FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%ETH%' AND b.is_active=1")
for r in q.fetchall(): print(' ', r)

print()
print("=== ETH - active_positions ===")
q.execute("SELECT bot_id,pair,side,size FROM active_positions WHERE pair LIKE '%ETH%'")
for r in q.fetchall(): print(' ', r)

print()
print("=== BTC - ALL ACTIVE BOT_ORDERS ===")
q.execute("""
    SELECT b.name, b.direction, bo.order_type, bo.status, bo.filled_amount, bo.cycle_id
    FROM bots b JOIN bot_orders bo ON b.id=bo.bot_id
    WHERE b.pair LIKE '%BTC%' AND b.is_active=1 
    AND bo.status NOT IN ('reset_cleared','auto_closed','cancelled','canceled')
    AND bo.filled_amount > 0
    ORDER BY b.id, bo.created_at DESC
""")
for r in q.fetchall(): print(' ', r)

print()
print("=== BTC - TRADES ===")
q.execute("SELECT b.id,b.name,b.direction,t.total_invested,t.avg_entry_price,t.current_step,t.cycle_id,t.entry_confirmed FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%BTC%' AND b.is_active=1")
for r in q.fetchall(): print(' ', r)

print()
print("=== BTC - active_positions ===")
q.execute("SELECT bot_id,pair,side,size FROM active_positions WHERE pair LIKE '%BTC%'")
for r in q.fetchall(): print(' ', r)

print()
print("=== ALL active_positions (for reference) ===")
q.execute("SELECT bot_id,pair,side,size FROM active_positions ORDER BY pair")
for r in q.fetchall(): print(' ', r)

c.close()
