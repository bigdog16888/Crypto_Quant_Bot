import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from engine.ledger import seal_all_active_bots

print("=== Before seal: ETH bot open_qty and total_invested ===")
conn = sqlite3.connect('crypto_bot.db')
before = conn.execute("""
    SELECT b.id, b.name, b.direction, b.status, t.open_qty, t.total_invested, t.cycle_id
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.pair LIKE '%ETH%USDC%' AND b.name NOT LIKE '%link%'
    ORDER BY b.name
""").fetchall()
for r in before:
    print(f"  id={r[0]:>7}  {r[1]:<18}  dir={r[2]:<6}  status={r[3]:<24}  open_qty={r[4]}  invested={r[5]}  cycle_id={r[6]}")
conn.close()

print()
print("=== Calling seal_all_active_bots() ===")
corrected = seal_all_active_bots()
print(f"  Bots resealed: {corrected}")

print()
print("=== After seal: ETH bot open_qty and total_invested ===")
conn2 = sqlite3.connect('crypto_bot.db')
after = conn2.execute("""
    SELECT b.id, b.name, b.direction, b.status, t.open_qty, t.total_invested, t.cycle_id
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.pair LIKE '%ETH%USDC%' AND b.name NOT LIKE '%link%'
    ORDER BY b.name
""").fetchall()
for r in after:
    print(f"  id={r[0]:>7}  {r[1]:<18}  dir={r[2]:<6}  status={r[3]:<24}  open_qty={r[4]}  invested={r[5]}  cycle_id={r[6]}")

print()
print("=== Virtual net contribution after seal ===")
net = conn2.execute("""
    SELECT b.id, b.name, b.direction, b.is_active,
           SUM(CASE
               WHEN bo.order_type IN ('entry','grid','adoption','adoption_add','carry') THEN bo.filled_amount
               WHEN bo.order_type IN ('tp','close','exit','adoption_reduce','dust_close','sl','virtual_netting') THEN -bo.filled_amount
               ELSE 0
           END) as virtual_contrib
    FROM bots b
    LEFT JOIN bot_orders bo ON bo.bot_id = b.id AND bo.filled_amount > 0
    WHERE (b.pair LIKE '%ETH%USDC%') AND b.name NOT LIKE '%link%'
    GROUP BY b.id, b.name, b.direction, b.is_active
    ORDER BY b.name
""").fetchall()
total_net = 0.0
for r in net:
    contrib = r[4] or 0.0
    signed = contrib if r[2] == 'LONG' else -contrib
    total_net += signed
    print(f"  id={r[0]:>7}  {r[1]:<18}  dir={r[2]:<6}  contrib={contrib:>10.6f}  signed={signed:>10.6f}")
print(f"  {'TOTAL VIRTUAL NET':>40}  {total_net:>10.6f}")

print()
print("=== Physical ETH positions ===")
phys = conn2.execute("SELECT bot_id, pair, side, size, entry_price FROM active_positions WHERE pair LIKE '%ETH%'").fetchall()
phys_net = 0.0
for r in phys:
    signed = r[3] if r[2] == 'LONG' else -r[3]
    phys_net += signed
    print(f"  bot_id={r[0]}  pair={r[1]}  side={r[2]}  size={r[3]}  entry={r[4]}  signed={signed}")
print(f"  {'TOTAL PHYSICAL NET':>40}  {phys_net:>10.6f}")

print()
print(f"  Diff (virtual - physical): {total_net - phys_net:+.6f}")
conn2.close()
