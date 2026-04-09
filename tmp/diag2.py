import sys
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
cursor = conn.cursor()

# Check what the Reconciler actually sees for ETH, BNB, SOL
for bid, pair in [(10011, 'ETH'), (10007, 'BNB'), (10008, 'SOL')]:
    print(f'=== Bot {bid} ({pair}) LEDGER ===')
    cursor.execute('''
        SELECT order_id, order_type, status, amount, filled_amount, price 
        FROM bot_orders 
        WHERE bot_id=? ORDER BY created_at DESC LIMIT 6
    ''', (bid,))
    for ro in cursor.fetchall():
        print(f"  {ro[1]} {ro[2]}: {ro[3]} {ro[4]} @ {ro[5]}")
        
    cursor.execute("SELECT total_invested, avg_entry_price, current_step, cycle_phase FROM trades WHERE bot_id=?", (bid,))
    tr = cursor.fetchone()
    if tr:
        print(f"  TRADES: inv=${tr[0]} avg={tr[1]} step={tr[2]} phase={tr[3]}")
    
print("=== EXC ===")
for r in conn.execute("SELECT pair, side, size FROM active_positions WHERE pair LIKE '%ETH%' OR pair LIKE '%BNB%' OR pair LIKE '%SOL%'").fetchall():
    print(f"  {r[0]} {r[1]} {r[2]}")

conn.close()
