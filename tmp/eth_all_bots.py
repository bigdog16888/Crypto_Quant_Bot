import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Which ETH bots exist and what are their current states?
print("=== ALL ETH USDC BOTS ===")
c.execute("""
    SELECT b.id, b.name, b.direction, t.total_invested, t.avg_entry_price, t.current_step, t.cycle_id
    FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.pair LIKE '%ETH%USDC%'
""")
bots = c.fetchall()
for b in bots:
    print(b)

# Get ALL order IDs across ALL ETH bots
print("\n=== Known OIDs per ETH bot ===")
c.execute("""
    SELECT bot_id, COUNT(*), SUM(filled_amount) 
    FROM bot_orders 
    WHERE bot_id IN (10011, 10021, 100002) AND order_id IS NOT NULL AND filled_amount > 0
    GROUP BY bot_id
""")
for r in c.fetchall():
    print(r)

# What's the virtual net across all ETH SHORT bots?
print("\n=== Virtual net per ETH bot (current cycle) ===")
for bot_id in [10011, 10021, 100002]:
    c.execute("SELECT cycle_id, total_invested, avg_entry_price FROM trades WHERE bot_id=?", (bot_id,))
    row = c.fetchone()
    if not row:
        continue
    cycle_id = row[0]
    c.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN order_type IN ('adoption_reduce','tp','close','dust_close','sl') THEN filled_amount ELSE 0 END), 0)
        FROM bot_orders WHERE bot_id=? AND cycle_id=? AND filled_amount > 0
        AND client_order_id LIKE 'CQB_%' AND status NOT IN ('placing','failed','auto_closed','reset_cleared')
    """, (bot_id, cycle_id))
    net = c.fetchone()[0]
    print(f"  bot {bot_id}: cycle={cycle_id}, virtual_net={net:.6f}")

conn.close()
