import sqlite3
c = sqlite3.connect('crypto_bot.db')

# Cross-reference each bot's step vs actual confirmed filled bot_orders
print("\n=== STEP CROSS-REFERENCE AUDIT ===\n")
bots_in_trade = c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price, t.cycle_id
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE t.total_invested > 0
    ORDER BY t.total_invested DESC
""").fetchall()

for row in bots_in_trade:
    bot_id, name, pair, direction, step, invested, avg_entry, cycle_id = row
    
    # Count actual filled orders this cycle (entries + grids)
    order_rows = c.execute("""
        SELECT order_type, filled_amount, price, step, status, client_order_id
        FROM bot_orders
        WHERE bot_id=? AND cycle_id=?
          AND order_type IN ('entry','grid','adoption_add','adoption','adoption_reduce')
          AND filled_amount > 0
          AND status NOT IN ('open','new','placing','failed','auto_closed','reset_cleared')
        ORDER BY step ASC
    """, (bot_id, cycle_id)).fetchall()
    
    # Compute ground truth from orders
    net_qty = 0.0
    net_cost = 0.0
    max_step = 0
    for otype, famt, fprice, fstep, fstatus, cid in order_rows:
        if otype in ('entry','grid','adoption_add','adoption'):
            net_qty += famt
            net_cost += famt * fprice
        elif otype == 'adoption_reduce':
            net_qty -= famt
            net_cost -= famt * fprice
        max_step = max(max_step, fstep or 0)
    
    net_qty = max(0.0, net_qty)
    net_cost = max(0.0, net_cost)
    
    db_qty = float(invested) / float(avg_entry) if float(avg_entry) > 0 else 0.0
    step_match = "✅" if int(step or 0) == max_step else f"⚠️ (db={step}, orders_max={max_step})"
    inv_match = "✅" if abs(net_cost - float(invested)) < 1.0 else f"⚠️ (db={float(invested):.2f}, orders_sum={net_cost:.2f}, diff={float(invested)-net_cost:.2f})"
    qty_match = "✅" if abs(net_qty - db_qty) < 0.01 else f"⚠️ (db_qty={db_qty:.4f}, orders_qty={net_qty:.4f})"
    
    print(f"Bot: {name} ({bot_id}) | Pair: {pair} | Dir: {direction} | Cycle: {cycle_id}")
    print(f"  Step:     {step_match}")
    print(f"  Invested: {inv_match}")
    print(f"  Qty:      {qty_match}")
    if order_rows:
        print(f"  Orders this cycle: {len(order_rows)} rows")
    else:
        print(f"  ⚠️  NO confirmed bot_orders found for this cycle ({cycle_id})")
    print()
