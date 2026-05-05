from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()

pair_to_analyze = 'XRPUSDC'

sql = """
    SELECT 
        o.id, o.cycle_id, t.cycle_id, o.status, o.order_type, o.filled_amount, b.direction,
        (CASE 
            WHEN (o.cycle_id = t.cycle_id OR (o.cycle_id IS NULL AND t.cycle_id IS NULL)) 
                 AND o.status NOT IN ('auto_closed', 'reset_cleared')
                 AND o.order_type IN ('entry', 'grid', 'adoption', 'adoption_add', 'forensic_adoption_add', 'carry') THEN (CASE WHEN b.direction = 'LONG' THEN o.filled_amount ELSE -o.filled_amount END)
            WHEN (o.cycle_id = t.cycle_id OR (o.cycle_id IS NULL AND t.cycle_id IS NULL))
                 AND o.status NOT IN ('auto_closed', 'reset_cleared')
                 AND o.order_type IN ('tp', 'close', 'sl', 'dust_close', 'adoption_reduce', 'forensic_adoption_reduce', 'virtual_netting') THEN (CASE WHEN b.direction = 'LONG' THEN -o.filled_amount ELSE o.filled_amount END)
            
            -- Hedges
            WHEN (o.cycle_id = t.cycle_id OR (o.cycle_id IS NULL AND t.cycle_id IS NULL))
                 AND o.status NOT IN ('auto_closed', 'reset_cleared')
                 AND o.order_type LIKE 'hedge%' AND o.order_type NOT LIKE '%tp%' THEN (CASE WHEN b.direction = 'LONG' THEN -o.filled_amount ELSE o.filled_amount END)
            WHEN (o.cycle_id = t.cycle_id OR (o.cycle_id IS NULL AND t.cycle_id IS NULL))
                 AND o.status NOT IN ('auto_closed', 'reset_cleared')
                 AND (o.order_type LIKE 'hedge%tp%' OR o.order_type LIKE 'hedgetp%') THEN (CASE WHEN b.direction = 'LONG' THEN o.filled_amount ELSE -o.filled_amount END)
            ELSE 0 END) as contribution,
        b.pair, o.bot_id
    FROM bot_orders o
    JOIN trades t ON o.bot_id = t.bot_id
    JOIN bots b ON o.bot_id = b.id
"""

cur.execute(sql)
rows = cur.fetchall()
print(f"Analyzing Pair: {pair_to_analyze}")
print("\nParity Engine Contribution:")
total = 0
for r in rows:
    row_pair = r[8].replace('/', '').replace(':', '').replace('-', '').upper()
    if row_pair == pair_to_analyze and abs(r[7]) > 0:
        print(f"  ID: {r[0]} | Bot: {r[9]} | Cyc: {r[1]} vs {r[2]} | Type: {r[4]} | Amt: {r[5]} | Dir: {r[6]} | Contr: {r[7]}")
        total += r[7]

print(f"\nTotal Parity Engine Result: {total}")
