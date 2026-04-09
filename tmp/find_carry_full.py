import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Full details on all carry orders
c.execute("SELECT id, order_type, amount, filled_amount, created_at, client_order_id, status, cycle_id FROM bot_orders WHERE bot_id=10018 AND client_order_id LIKE '%CARRY%'")
for r in c.fetchall():
    print(r)
    
print()

# Also check what cycle_id the trades table thinks is current for this bot
c.execute("SELECT bot_id, cycle_id, total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=10018")
for r in c.fetchall():
    print("TRADES:", r)

conn.close()
