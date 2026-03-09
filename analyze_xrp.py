import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
cycle_id = c.fetchone()[0]
print(f"XRP Bot 10017 cycle_id: {cycle_id}")

c.execute("""
    SELECT order_id, client_order_id, order_type, amount, price, status, notes
    FROM bot_orders WHERE bot_id=10017 AND cycle_id=?
    ORDER BY created_at ASC
""", (cycle_id,))

rows = c.fetchall()
total_cost = 0.0
print("\nBOT_ORDERS for Bot 10017 (current cycle):")
print("-" * 100)
for r in rows:
    cost = (r[3] or 0) * (r[4] or 0)
    if r[5] == 'filled':
        total_cost += cost
    print(f"Status: {r[5]:12} | Type: {r[2]:6} | Amt: {r[3]:.4f} | Price: {r[4]:.5f} | Cost: ${cost:.2f} | OID: {str(r[0])[:30]} | CID: {str(r[1])[:35]}")

print(f"\nSUM of all 'filled' order costs: ${total_cost:.2f}")

c.execute("SELECT total_invested, current_step FROM trades WHERE bot_id=10017")
row = c.fetchone()
print(f"DB trades.total_invested: ${row[0]:.2f} | current_step: {row[1]}")

conn.close()
