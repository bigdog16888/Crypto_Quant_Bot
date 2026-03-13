"""
Quick SUI mismatch analysis.
Exchange short: -28,541.1 SUI
DB: short_sui=63,455.4, long_sui=34,625.8 → net short = 28,829.6 SUI
Diff: ~288.5 SUI ≈ $297
"""
import sqlite3

conn = sqlite3.connect('quant_bot.db')
c = conn.cursor()

print("=== TRADES TABLE ===")
c.execute("SELECT bot_id, total_invested, avg_entry_price, current_step FROM trades WHERE bot_id IN (SELECT id FROM bots WHERE pair='SUI/USDC:USDC')")
for row in c.fetchall():
    qty = row[1] / row[2] if row[2] else 0
    print(f"  bot_id={row[0]}, step={row[3]}, invested={row[1]:.2f}, avg_price={row[2]:.5f}, qty={qty:.2f}")

print("\n=== BOT_ORDERS (SUI, status not empty, filled_amount > 0) ===")
c.execute("""
    SELECT bo.bot_id, bo.order_type, bo.order_id, bo.status, bo.amount, bo.filled_amount, bo.step, bo.price
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair='SUI/USDC:USDC' AND bo.filled_amount > 0
    ORDER BY bo.bot_id, bo.step, bo.order_type
""")
for row in c.fetchall():
    print(f"  bot={row[0]}, type={row[1]},  order={row[2]}, status={row[3]}, amt={row[4]:.2f}, filled={row[5]:.2f}, step={row[6]}, price={row[7]:.4f}")

print("\n=== NET FILLED BY BOT (SUI orders) ===")
c.execute("""
    SELECT bo.bot_id, b.name, b.direction, SUM(bo.filled_amount) as total_filled
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair='SUI/USDC:USDC' AND bo.filled_amount > 0 AND bo.order_type IN ('grid','entry','TP','tp')
    GROUP BY bo.bot_id, b.direction
""")
for row in c.fetchall():
    print(f"  bot_id={row[0]}, name={row[1]}, dir={row[2]}, total_filled={row[3]:.2f}")

conn.close()
