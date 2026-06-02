import sqlite3

conn = sqlite3.connect('crypto_bot.db')

print("=== QUERY 1: ETH bots state ===")
rows = conn.execute("""
    SELECT b.id, b.name, b.is_active, b.status, b.bot_type,
           b.parent_bot_id, t.open_qty, t.total_invested,
           t.cycle_id, t.cycle_phase, t.entry_confirmed
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.pair LIKE '%ETH%USDC%'
    AND b.name NOT LIKE '%link%'
    ORDER BY b.name
""").fetchall()

cols = ['id','name','is_active','status','bot_type','parent_bot_id',
        'open_qty','total_invested','cycle_id','cycle_phase','entry_confirmed']
print(f"{'id':>7}  {'name':<18}  {'is_active':>9}  {'status':<24}  {'bot_type':<12}  "
      f"{'parent_bot_id':>13}  {'open_qty':>10}  {'total_invested':>14}  "
      f"{'cycle_id':>8}  {'cycle_phase':<12}  {'entry_confirmed':>15}")
print("-" * 160)
for r in rows:
    print(f"{r[0]:>7}  {str(r[1]):<18}  {str(r[2]):>9}  {str(r[3]):<24}  {str(r[4]):<12}  "
          f"{str(r[5]):>13}  {str(r[6]):>10}  {str(r[7]):>14}  "
          f"{str(r[8]):>8}  {str(r[9]):<12}  {str(r[10]):>15}")

print()
print("=== QUERY 2: eth_hedge is_active ===")
row = conn.execute("""
    SELECT b.id, b.name, b.is_active, b.status, b.bot_type, b.pair,
           b.direction, b.parent_bot_id
    FROM bots b
    WHERE b.name = 'eth_hedge'
""").fetchone()
if row:
    print(f"  id={row[0]}  name={row[1]!r}  is_active={row[2]}  status={row[3]!r}  "
          f"bot_type={row[4]!r}  pair={row[5]!r}  direction={row[6]!r}  parent_bot_id={row[7]}")
else:
    print("  NOT FOUND")

print()
print("=== QUERY 3: Last 5 bot_orders for eth_hedge (id=100316) ===")
orders = conn.execute("""
    SELECT bo.id, bo.order_id, bo.client_order_id, bo.order_type,
           bo.filled_amount, bo.amount, bo.price, bo.status,
           bo.step, bo.cycle_id,
           datetime(bo.created_at, 'unixepoch') as created,
           bo.position_side, bo.notes
    FROM bot_orders bo
    WHERE bo.bot_id = 100316
    ORDER BY bo.created_at DESC
    LIMIT 5
""").fetchall()

if orders:
    for o in orders:
        print(f"  row_id={o[0]}  order_id={o[1]!r}  cid={o[2]!r}")
        print(f"    type={o[3]!r}  filled={o[4]}  amount={o[5]}  price={o[6]}")
        print(f"    status={o[7]!r}  step={o[8]}  cycle_id={o[9]}")
        print(f"    created={o[10]}  position_side={o[11]!r}")
        print(f"    notes={o[12]!r}")
        print()
else:
    print("  NO ROWS FOUND")

print()
print("=== QUERY 4: get_pair_virtual_net components — what contributes to ETH net ===")
net_rows = conn.execute("""
    SELECT b.id, b.name, b.direction, b.is_active,
           SUM(CASE
               WHEN bo.order_type IN ('entry','grid','adoption','adoption_add','carry') THEN bo.filled_amount
               WHEN bo.order_type IN ('tp','close','exit','adoption_reduce','dust_close','sl','virtual_netting') THEN -bo.filled_amount
               ELSE 0
           END) as virtual_contrib
    FROM bots b
    LEFT JOIN bot_orders bo ON bo.bot_id = b.id AND bo.filled_amount > 0
    WHERE (b.pair LIKE '%ETH%USDC%' OR b.pair LIKE '%ETHUSDC%')
    AND b.name NOT LIKE '%link%'
    GROUP BY b.id, b.name, b.direction, b.is_active
    ORDER BY b.name
""").fetchall()

print(f"  {'id':>7}  {'name':<18}  {'direction':<10}  {'is_active':>9}  {'virtual_contrib':>15}")
print("  " + "-" * 70)
for r in net_rows:
    print(f"  {r[0]:>7}  {str(r[1]):<18}  {str(r[2]):<10}  {str(r[3]):>9}  {str(r[4] or 0):>15}")

conn.close()
