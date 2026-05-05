from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%SUI%'")
bots = cur.fetchall()
print("Bots:", bots)

for b_id, name, pair, direction, is_active in bots:
    print(f"\n--- {name} (ID: {b_id}) ---")
    cur.execute("SELECT cycle_id, open_qty, total_invested, avg_entry_price, hedge_qty FROM trades WHERE bot_id=?", (b_id,))
    print("Trade State:", cur.fetchone())
    
    cur.execute("SELECT id, cycle_id, order_type, amount, filled_amount, status, client_order_id FROM bot_orders WHERE bot_id=? AND filled_amount > 0 AND status NOT IN ('canceled', 'rejected')", (b_id,))
    orders = cur.fetchall()
    print(f"Filled Orders ({len(orders)}):")
    for o in orders:
        print(f"  {o}")

cur.execute("SELECT * FROM active_positions WHERE pair LIKE '%SUI%'")
print("\nActive Positions (Snapshot):", cur.fetchall())
