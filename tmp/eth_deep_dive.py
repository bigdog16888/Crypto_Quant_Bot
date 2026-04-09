import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Full ETH SHORT bot orders - show all filled entries and exits
print("=== ETH bot 10011 ALL filled orders ===")
c.execute("""
    SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id, cycle_id
    FROM bot_orders 
    WHERE bot_id=10011 AND filled_amount > 0
    ORDER BY created_at DESC LIMIT 30
""")
for r in c.fetchall():
    print(r)

print("\n=== ETH trades row (virtual ledger) ===")
c.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=10011")
print(c.fetchone())

print("\n=== ETH recompute simulation ===")
c.execute("""
    SELECT 
        COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END), 0) -
        COALESCE(SUM(CASE WHEN order_type IN ('adoption_reduce','tp','close','dust_close','sl') THEN filled_amount ELSE 0 END), 0)
    FROM bot_orders
    WHERE bot_id=10011 AND filled_amount > 0
    AND cycle_id = (SELECT cycle_id FROM trades WHERE bot_id=10011)
    AND client_order_id LIKE 'CQB_%'
    AND status NOT IN ('placing','failed','auto_closed','reset_cleared')
""")
print("Virtual net qty:", c.fetchone()[0])

conn.close()
