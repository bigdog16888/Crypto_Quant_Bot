import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT 
        COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount ELSE 0 END), 0.0) 
        - COALESCE(SUM(CASE WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'sl', 'dust_close') THEN filled_amount ELSE 0 END), 0.0)
    FROM bot_orders 
    WHERE bot_id=10018 AND cycle_id=1 
    AND created_at < 1774859501
    AND filled_amount > 0
""")
total = float(c.fetchone()[0])
print(f"Total SUI qty in cycle 1 before 16:31:41 yesterday: {total}")

conn.close()
