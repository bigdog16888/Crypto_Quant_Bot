import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT step, order_type, filled_amount, price, status, cycle_id 
    FROM bot_orders 
    WHERE bot_id=10018 AND cycle_id=1 
    AND status NOT IN ('placing', 'failed', 'auto_closed', 'reset_cleared')
    ORDER BY created_at ASC
""")
rows = c.fetchall()

total_qty = 0.0
for r in rows:
    step, otype, filled, price, status, cycle = r
    filled = float(filled) if filled else 0.0
    if filled > 0:
        if otype in ('entry', 'grid', 'adoption_add', 'adoption'):
            total_qty += filled
        elif otype in ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl'):
            total_qty -= filled
            
        print(f"{otype:10} | {filled:10.4f} | {status:15} | Running Qty: {total_qty:.4f}")

conn.close()
