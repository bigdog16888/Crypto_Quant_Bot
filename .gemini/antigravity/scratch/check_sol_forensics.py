import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== Two live entry orders above wipe wall ===")
cur.execute("SELECT id, order_id, client_order_id, status, filled_amount, price, notes FROM bot_orders WHERE id IN (92536, 92557)")
for r in cur.fetchall():
    print(dict(r))

print("\n=== active_positions for XAU ===")
cur.execute("SELECT * FROM active_positions WHERE pair LIKE '%XAU%'")
for r in cur.fetchall():
    print(dict(r))

print("\n=== CONCLUSION ===")
print("order 92536 is a REAL fill (order_id from exchange) - 0.005 XAU short")
print("order 92557 is a 'history-orphan' - was it SAME exchange order as 92536?")
cur.execute("SELECT order_id FROM bot_orders WHERE id=92536")
oid1 = cur.fetchone()['order_id']
cur.execute("SELECT order_id FROM bot_orders WHERE id=92557")
oid2 = cur.fetchone()['order_id']
print(f"  92536 order_id: {oid1}")
print(f"  92557 order_id: {oid2}")
if oid1 == oid2:
    print("  => SAME ORDER! History-orphan duplicated the fill. open_qty overcounted by 0.005!")
else:
    print(f"  => Different orders. Both are genuine fills but exchange only shows 0.005 physical.")
    print(f"  => One of them may have been TP'd already (closed before current cycle start)")

conn.close()
