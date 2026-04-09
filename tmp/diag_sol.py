import sys
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

# 1. SOL Bots Ledger
print('=== SOL BOTS LEDGER ===')
rows = conn.execute("""
    SELECT b.id, b.name, b.direction, b.status,
           COALESCE(t.total_invested,0), COALESCE(t.avg_entry_price,0), COALESCE(t.current_step,0)
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.pair LIKE '%SOL%' AND b.is_active=1
""").fetchall()

for row in rows:
    bid, name, dirn, status, inv, avg, step = row
    qty = float(inv)/float(avg) if float(avg)>0 else 0
    print(f"Bot {bid} ({name}) | {dirn} | inv=${float(inv):.2f} qty={qty:.4f} step={step} | {status}")

# 2. SOL Active Positions (Exchange)
print('\n=== SOL EXCHANGE POSITIONS ===')
ap = conn.execute("SELECT side, size, entry_price, bot_id FROM active_positions WHERE pair LIKE '%SOL%'").fetchall()
for side, size, price, bid in ap:
    print(f"EXC: {side} size={float(size):.4f} bot_id={bid}")

# 3. SUI bot status
print('\n=== SUI BOT 10018 STATUS ===')
sui = conn.execute("SELECT status, is_active FROM bots WHERE id=10018").fetchone()
print(f"SUI Bot 10018: status={sui[0]} is_active={sui[1]}")

conn.close()
