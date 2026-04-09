import sqlite3, json

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== BOT CONFIG + CURRENT STEP + PHYSICAL vs INVESTED ===")
print("Goal: understand how to correctly derive current_step from phys_qty\n")

q.execute("""
    SELECT b.id, b.name, b.direction, b.base_size, b.martingale_multiplier,
           b.strategy_type, t.total_invested, t.avg_entry_price, t.current_step, t.cycle_id,
           ap.size as phys_qty, ap.entry_price
    FROM bots b
    LEFT JOIN trades t ON b.id=t.bot_id
    LEFT JOIN active_positions ap ON ap.bot_id=b.id
    WHERE b.is_active=1
    ORDER BY b.id
""")
rows = q.fetchall()

for r in rows:
    bid, name, direction, base_size, mm, strat_json, ti, avg, step, cyc, phys, entry_p = r
    if not ti: continue
    
    try:
        strat = json.loads(strat_json) if strat_json else {}
    except:
        strat = {}
    
    # How many steps would make sense at this investment level?
    # Each step = base_size * martingale^(step-1) 
    # Total invested after N steps = sum(base_size * mm^i for i in 0..N-1)
    if base_size and mm and mm > 0 and ti > 0:
        cumulative = 0
        estimated_step = 0
        for n in range(1, 20):
            step_cost = base_size * (mm ** (n - 1))
            cumulative += step_cost
            estimated_step = n
            if cumulative >= ti * 0.95:  # within 5%
                break
        step_flag = "✅" if abs(step - estimated_step) <= 1 else f"❌ DB={step} should≈{estimated_step}"
    else:
        estimated_step = "?"
        step_flag = "?"
    
    print(f"  Bot {bid} ({name} {direction}):")
    print(f"    base_size={base_size}, mm={mm}, max_steps={strat.get('MaxSteps','?')}")
    print(f"    DB: step={step}, ti=${ti:.2f}, avg=${avg:.4f}")
    print(f"    Physical: qty={phys}, entry=${entry_p:.6f}")
    print(f"    Step est from ti: {estimated_step} {step_flag}")
    if phys and avg:
        print(f"    System qty = ti/avg = {ti/avg:.4f} vs phys={phys}")
    print()

c.close()
