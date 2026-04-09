import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== CURRENT TRADES STATE ===")
for bid, name in [(10016, 'long_btc_price'), (10022, 'short_btc'), (10008, 'sol'), (10017, 'xrp_long')]:
    q.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id, entry_confirmed FROM trades WHERE bot_id=?", (bid,))
    r = q.fetchone()
    if r:
        ti, avg, step, cyc, conf = r
        vqty = ti/avg if avg else 0
        print(f"  Bot {bid} ({name}): ti=${ti:.2f} avg={avg:.4f} vqty={vqty:.4f} cycle={cyc} confirmed={conf}")

print()
print("=== BTC cycle=10 current bot_orders ===")
q.execute("""
    SELECT order_type, status, filled_amount, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=10
    AND status NOT IN ('placing','failed')
    ORDER BY created_at
""")
for r in q.fetchall(): print(f"  {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} [{r[4][:55]}]")

print()
print("=== SOL: Is entry_confirmed the blocker? ===")
q.execute("SELECT entry_confirmed, total_invested, cycle_id FROM trades WHERE bot_id=10008")
r = q.fetchone()
print(f"  SOL bot 10008: confirmed={r[0]}, total_invested={r[1]}, cycle={r[2]}")
q.execute("SELECT bot_id, size FROM active_positions WHERE pair='SOLUSDC'")
r2 = q.fetchone()
print(f"  active_positions: bot_id={r2[0] if r2 else '?'}, size={r2[1] if r2 else '?'}")

print()
print("=== XRP current adoption ===")
q.execute("""
    SELECT order_type, status, filled_amount, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10017 AND order_type='adoption' AND status='filled'
""")
for r in q.fetchall(): print(f"  {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} cycle={r[3]} [{r[4][:55]}]")

print()
print("=== RECONCILER CODE check: look for entry_confirmed filter ===")
# Check if reconciler filters by entry_confirmed
import re
with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    content = f.read()
    
# Find references to entry_confirmed in reconciler  
lines = content.split('\n')
for i, line in enumerate(lines):
    if 'entry_confirmed' in line and i < 300:  # Only first 300 lines (bot selection area)
        print(f"  L{i+1}: {line.strip()}")

c.close()
