import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== MARTINGALE STEP INVERSION VERIFICATION ===")
print("Simulates exactly what PASS-3 will compute on next reconciler run\n")

q.execute("""
    SELECT b.id, b.name, b.direction, b.base_size, b.martingale_multiplier,
           t.total_invested, t.avg_entry_price, t.current_step, t.cycle_id,
           ap.size as phys_qty, ap.entry_price
    FROM bots b
    LEFT JOIN trades t ON b.id=t.bot_id
    LEFT JOIN active_positions ap ON ap.bot_id=b.id AND ap.size > 0
    WHERE b.is_active=1 AND ap.size > 0
    ORDER BY b.id
""")

for r in q.fetchall():
    bid, name, direction, base_sz, mm, ti, avg, step, cyc, phys_qty, entry_p = r
    if not phys_qty: continue
    
    base_sz = float(base_sz or 0)
    mm = float(mm or 2.0)
    mm = mm if mm > 0 else 2.0
    ticker_price = float(entry_p or 0)
    phys_invested = phys_qty * ticker_price
    
    if base_sz > 0:
        cumul = 0.0
        est_n = 1
        for n in range(1, 30):
            step_cost = base_sz * (mm ** (n - 1))
            cumul += step_cost
            est_n = n
            if cumul >= phys_invested * 0.90:
                break
        
        ok = "✅" if est_n > 1 or phys_invested < base_sz * 1.2 else "⚠️"
        
        # Sanity check: actual step from DB
        db_step = int(step or 0)
        step_flag = "✅" if abs(db_step - est_n) <= 1 else f"❌ DB={db_step} est={est_n}"
        
        print(f"  {name} ({direction}):")
        print(f"    base=${base_sz} mm={mm} phys_inv=${phys_invested:.2f}")
        print(f"    Martingale inversion: cumul=${cumul:.2f} → step={est_n} {ok}")
        print(f"    Current DB step={db_step} {step_flag}")
        print(f"    Expected after restart: step={max(est_n, db_step)} ← correct {ok}")
        print()

c.close()
