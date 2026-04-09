import sqlite3
c = sqlite3.connect('crypto_bot.db')

print("=== SOL BOT 10008 - Cycle 46 Detail ===")
rows = c.execute("""
    SELECT order_type, step, filled_amount, price, status, client_order_id, created_at
    FROM bot_orders
    WHERE bot_id=10008 AND cycle_id=46
    ORDER BY created_at ASC
""").fetchall()
print(f"{'Type':15} {'Step':>5} {'Qty':>10} {'Price':>12} {'Status':15} {'CID':40}")
print("-"*100)
gross_add = 0.0
gross_cost = 0.0
for otype, step, famt, fprice, fstatus, cid, ts in rows:
    if famt and famt > 0 and fprice and fprice > 0:
        sign = "+" if otype in ('entry','grid','adoption_add','adoption') else "-"
        if otype in ('entry','grid','adoption_add','adoption'):
            gross_add += famt
            gross_cost += famt * fprice
        elif otype == 'adoption_reduce':
            gross_add -= famt
            gross_cost -= famt * fprice
        print(f"{otype:15} {str(step or ''):>5} {sign}{famt:>9.4f} {fprice:>12.4f} {fstatus:15} {str(cid)[:40]}")
    else:
        print(f"{otype:15} {str(step or ''):>5} {str(famt or 0):>10} {str(fprice or 0):>12} {fstatus:15} {str(cid)[:40]}")

print()
print(f"Gross NET Qty: {gross_add:.4f} | Gross NET Cost: ${gross_cost:.2f}")
print(f"DB says: total_invested=1513.58, qty=16.6500")
print(f"Difference: qty_diff={16.65 - gross_add:.4f}, cost_diff={1513.58 - gross_cost:.2f}")
