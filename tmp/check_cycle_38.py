import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT 
        COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount ELSE 0 END), 0) -
        COALESCE(SUM(CASE WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0)
    FROM bot_orders 
    WHERE bot_id=10018 AND cycle_id=38 AND filled_amount > 0
    AND status NOT IN ('reset_cleared', 'auto_closed')
""")
print(f"Cycle 38 Net Qty: {c.fetchone()[0]}")

conn.close()
