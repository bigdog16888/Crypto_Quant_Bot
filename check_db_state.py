"""Quick database state check"""
from engine.database import get_connection

conn = get_connection()
cur = conn.cursor()

print("=== ACTIVE BOTS ===")
for r in cur.execute('SELECT id, name, pair, direction, is_active FROM bots WHERE is_active=1').fetchall():
    print(f"Bot {r[0]}: {r[1]} | {r[2]} {r[3]}")

print("\n=== TRADES (In Position) ===")
for r in cur.execute('SELECT bot_id, current_step, total_invested, avg_entry_price FROM trades WHERE total_invested > 0').fetchall():
    print(f"Bot {r[0]}: Step {r[1]} | Invested: ${r[2]:.2f} | Entry: ${r[3]:.4f}")

print("\n=== BOT_ORDERS (Open) ===")
for r in cur.execute("SELECT bot_id, order_type, order_id, status FROM bot_orders WHERE status='open' ORDER BY bot_id").fetchall():
    print(f"Bot {r[0]}: {r[1]} | {r[2]} | Status: {r[3]}")

print("\n=== BOT_ORDERS COUNT BY BOT ===")
for r in cur.execute("SELECT bot_id, COUNT(*) FROM bot_orders WHERE status='open' GROUP BY bot_id").fetchall():
    print(f"Bot {r[0]}: {r[1]} open orders")
