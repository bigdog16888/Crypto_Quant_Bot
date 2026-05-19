import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

print("=" * 100)
print("QUERY 1: Do orderIds 189751954 and 189763240 exist ANYWHERE in bot_orders?")
print("=" * 100)
cursor.execute("""
SELECT order_id, order_type, filled_amount, price, status, 
       cycle_id, created_at, updated_at, notes, client_order_id
FROM bot_orders
WHERE order_id IN ('189751954', '189763240');
""")
rows = cursor.fetchall()
if not rows:
    print("  RESULT: ZERO ROWS — these orderIds do not exist in bot_orders at all.")
    print("  => The orders were placed on Binance without ever writing a receipt to the DB.")
else:
    for r in rows:
        print(f"  order_id={r[0]}")
        print(f"    order_type   : {r[1]}")
        print(f"    filled_amount: {r[2]}")
        print(f"    price        : {r[3]}")
        print(f"    status       : {r[4]}")
        print(f"    cycle_id     : {r[5]}")
        print(f"    created_at   : {r[6]}")
        print(f"    updated_at   : {r[7]}")
        print(f"    notes        : {r[8]}")
        print(f"    cid          : {r[9]}")
        print()

print()
print("=" * 100)
print("QUERY 2: All bot 100001 rows in the exact window ts 1778736000–1778739000")
print("=" * 100)
cursor.execute("""
SELECT order_id, order_type, filled_amount, price, status,
       cycle_id, created_at, updated_at, notes, client_order_id  
FROM bot_orders
WHERE bot_id = 100001
  AND created_at BETWEEN 1778736000 AND 1778739000
ORDER BY created_at ASC;
""")
rows2 = cursor.fetchall()
if not rows2:
    print("  RESULT: ZERO ROWS in this timestamp window for bot 100001.")
else:
    for r in rows2:
        print(f"  order_id={r[0]} | type={r[1]:20} | filled={r[2]:.4f} | price={r[3]:.4f} | status={r[4]:15} | cycle={r[5]} | ts={r[6]} | cid={r[9]}")
        if r[8]:
            print(f"    notes: {r[8]}")

print()
print("=" * 100)
print("SUPPLEMENTAL: Any bot_orders row where notes or cid references 189751954 or 189763240")
print("=" * 100)
cursor.execute("""
SELECT order_id, order_type, filled_amount, status, cycle_id, created_at, notes, client_order_id
FROM bot_orders
WHERE notes LIKE '%189751954%' OR notes LIKE '%189763240%'
   OR client_order_id LIKE '%189751954%' OR client_order_id LIKE '%189763240%'
""")
rows3 = cursor.fetchall()
if not rows3:
    print("  NONE — no cross-references in notes or client_order_id either.")
else:
    for r in rows3:
        print(f"  order_id={r[0]} | type={r[1]} | filled={r[2]} | status={r[3]} | cycle={r[4]} | ts={r[5]}")
        print(f"    notes={r[6]}")
        print(f"    cid={r[7]}")

conn.close()
