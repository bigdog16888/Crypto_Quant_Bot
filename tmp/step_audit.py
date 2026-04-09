import sqlite3
c = sqlite3.connect('crypto_bot.db')
rows = c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status,
           t.current_step, t.total_invested, t.avg_entry_price,
           t.cycle_id
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY t.total_invested DESC
""").fetchall()
print(f"{'ID':>6} {'Name':15} {'Pair':10} {'Dir':6} {'Status':12} {'Step':>5} {'Invested':>12} {'AvgEntry':>10} {'Cycle':>6}")
print("-"*80)
for r in rows:
    bid, name, pair, direction, status, step, invested, entry, cycle = r
    print(f"{bid:>6} {str(name):15} {str(pair):10} {str(direction):6} {str(status):12} {str(step or 0):>5} {float(invested or 0):>12.2f} {float(entry or 0):>10.4f} {str(cycle):>6}")
