import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()
c.execute("SELECT b.id, b.name, b.direction, b.config, t.total_invested, t.avg_entry_price, t.current_step FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%XAU%'")
r = c.fetchone()
if not r:
    print("No XAU bot found with open trade")
    exit()

bot_id, name, direction, cfg_raw, inv, entry, step = r
cfg = json.loads(cfg_raw)

print(f"Bot: {name} | Dir: {direction} | Step: {step}")
print(f"  avg_entry_price: {entry}")
print(f"  total_invested:  {inv}")
print()
print("Grid-related config:")
for k in ['UseATRGrid', 'ATRGridFactor', 'ATRPeriods', 'base_grid', 'StepPct', 'grid_dist_pct', 'GridMultiplier']:
    print(f"  {k}: {cfg.get(k)}")
print()
print("SYNC-DRIFT related config:")
for k in ['UseEarlyExit', 'TakeProfitPct', 'TakeProfitBase', 'TakeProfitType', 'DecayIntervalMins', 'DecayPercentPerInterval']:
    print(f"  {k}: {cfg.get(k)}")

# Also check its open orders in DB
c.execute("SELECT order_type, order_side, price, qty, status, client_order_id FROM bot_orders WHERE bot_id=? AND status='open'", (bot_id,))
orders = c.fetchall()
print(f"\nOpen DB orders for bot {bot_id}:")
for o in orders:
    print(f"  {o}")

conn.close()
