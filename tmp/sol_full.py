import sqlite3
c = sqlite3.connect('crypto_bot.db')

print("=== SOL BOT 10008 - Cycle 46 ALL Orders ===\n")
rows = c.execute("""
    SELECT order_type, step, amount, filled_amount, price, status, client_order_id, created_at
    FROM bot_orders
    WHERE bot_id=10008 AND cycle_id=46
    ORDER BY created_at ASC
""").fetchall()

total_added = 0.0
total_reduced = 0.0
for otype, step, amt, famt, fprice, fstatus, cid, ts in rows:
    famt = famt or 0.0
    fprice = fprice or 0.0
    amt = amt or 0.0
    cost = famt * fprice
    marker = ""
    if otype in ('entry','grid','adoption_add','adoption') and famt > 0:
        total_added += cost
        marker = f"  -> +${cost:.2f}"
    elif otype == 'adoption_reduce' and famt > 0:
        total_reduced += cost
        marker = f"  -> -${cost:.2f}"
    print(f"{str(ts):12} {otype:15} step={str(step or '?'):>3} filled={famt:.4f} price={fprice:.4f} status={fstatus:15} {marker}")

print()
print(f"Total ADDED notional from orders: ${total_added:.2f}")
print(f"Total REDUCED notional from orders: ${total_reduced:.2f}")
print(f"Net orders: ${total_added - total_reduced:.2f}")
print()
print("DB trade_history for SOL 10008:")
hist = c.execute("""
    SELECT action, amount, price, timestamp
    FROM trade_history
    WHERE bot_id=10008
    ORDER BY timestamp DESC LIMIT 10
""").fetchall()
for h in hist:
    print(f"  {h[3]}: {h[0]} qty={h[1]:.4f} @ ${h[2]:.4f}")
