import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== SOL all recent bot_orders ===")
q.execute("SELECT order_type,status,filled_amount,cycle_id,created_at FROM bot_orders WHERE bot_id=10008 ORDER BY created_at DESC LIMIT 20")
for r in q.fetchall():
    print(' ', r)

print()
print("=== SOL: virtual qty query (what _prepare_tp sees) ===")
q.execute("SELECT cycle_id FROM trades WHERE bot_id=10008")
cycle_id = q.fetchone()[0]
print(f"  cycle_id from trades: {cycle_id}")
q.execute("""
    SELECT 
        order_type,status,filled_amount,cycle_id,
        CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN 'ENTRY' ELSE 'EXIT' END as side
    FROM bot_orders
    WHERE bot_id=10008
    AND status NOT IN ('reset_cleared','auto_closed')
    AND (cycle_id=? OR cycle_id IS NULL)
    AND filled_amount>0
""", (cycle_id,))
rows = q.fetchall()
print(f"  Matching orders for cycle {cycle_id}:", rows if rows else "(NONE!)")

print()
print("=== short_eth: virtual qty query ===")
q.execute("SELECT cycle_id FROM trades WHERE bot_id=100002")
cycle_id_eth = q.fetchone()[0]
q.execute("""
    SELECT order_type,status,filled_amount,cycle_id 
    FROM bot_orders WHERE bot_id=100002 
    AND status NOT IN ('reset_cleared','auto_closed')
    AND (cycle_id=? OR cycle_id IS NULL) AND filled_amount>0
""", (cycle_id_eth,))
for r in q.fetchall(): print(' ', r)

c.close()
