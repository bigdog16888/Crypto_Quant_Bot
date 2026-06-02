import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from engine.ledger import seal_trade_state
from engine.database import get_pair_virtual_net

print("=== Before: trades row for short eth (100002) ===")
conn = sqlite3.connect('crypto_bot.db')
row = conn.execute(
    "SELECT open_qty, total_invested, avg_entry_price, cycle_id, entry_confirmed "
    "FROM trades WHERE bot_id = 100002"
).fetchone()
print(f"  open_qty={row[0]}  total_invested={row[1]}  avg_entry_price={row[2]}  cycle_id={row[3]}  entry_confirmed={row[4]}")
conn.close()

print()
print("=== Calling seal_trade_state(100002) ===")
seal_trade_state(100002)
print("  Done.")

print()
print("=== After: trades row for short eth (100002) ===")
conn2 = sqlite3.connect('crypto_bot.db')
row2 = conn2.execute(
    "SELECT open_qty, total_invested, avg_entry_price, cycle_id, entry_confirmed, status "
    "FROM trades t JOIN bots b ON b.id = t.bot_id WHERE t.bot_id = 100002"
).fetchone()
print(f"  open_qty={row2[0]}  total_invested={row2[1]}  avg_entry_price={row2[2]}  cycle_id={row2[3]}  entry_confirmed={row2[4]}")

print()
print("=== get_pair_virtual_net('ETHUSDC') after seal ===")
net = get_pair_virtual_net('ETHUSDC')
print(f"  virtual net = {net}")

print()
print("=== active_positions ETHUSDC (physical) ===")
phys = conn2.execute(
    "SELECT pair, side, size, entry_price, bot_id FROM active_positions WHERE pair = 'ETHUSDC'"
).fetchall()
phys_net = 0.0
for r in phys:
    signed = r[2] if r[1] == 'LONG' else -r[2]
    phys_net += signed
    print(f"  pair={r[0]}  side={r[1]}  size={r[2]}  entry={r[3]}  bot_id={r[4]}  signed={signed:+.6f}")
if not phys:
    print("  NO ROWS")
print(f"  Physical net = {phys_net:+.6f}")

print()
print(f"  Virtual: {net:+.6f}  Physical: {phys_net:+.6f}  Diff: {net - phys_net:+.6f}")

conn2.close()
