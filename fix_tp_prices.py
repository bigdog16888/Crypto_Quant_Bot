import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.strategies.martingale_strategy import MartingaleStrategy

conn = get_connection()
c = conn.cursor()

# Get ALL needed fields including total_invested
c.execute("""
    SELECT b.id, b.name, b.direction, b.config,
           t.total_invested, t.avg_entry_price, t.target_tp_price, t.current_step
    FROM bots b JOIN trades t ON b.id = t.bot_id
    WHERE t.total_invested > 0
""")
rows = c.fetchall()

print(f"{'ID':>6} {'Name':<22} {'Type':<8} {'Entry':>10} {'DB TP':>10} {'NewTP':>10} {'%':>7} {'Action'}")
print("-" * 85)

for r in rows:
    bid, name, direction, config_raw = r[0], r[1], r[2], r[3]
    invested, entry, db_tp, step = r[4], r[5], r[6], r[7]
    cfg = json.loads(config_raw) if config_raw else {}

    bot_status = {
        'total_invested': invested, 'avg_entry_price': entry,
        'target_tp_price': db_tp, 'current_step': step, 'direction': direction
    }

    try:
        strategy = MartingaleStrategy(cfg)
        new_tp = strategy.calculate_take_profit_price(bot_status, entry)
    except Exception as e:
        print(f"  ERROR bot {bid}: {e}")
        continue

    tp_type = cfg.get('TakeProfitType', 'Percent')
    pct = (new_tp - entry) / entry * 100 if entry > 0 else 0
    drift = abs(new_tp - db_tp) / max(db_tp, 0.0001)
    action = "UPDATE" if drift > 0.005 else "OK"

    print(f"{bid:>6} {name:<22} {tp_type:<8} {entry:>10.4f} {db_tp:>10.4f} {new_tp:>10.4f} {pct:>7.2f}% {action}")

    if action == "UPDATE":
        c.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (new_tp, bid))
        print(f"       → Updated DB: {db_tp:.4f} → {new_tp:.4f}")

conn.commit()
conn.close()
print("\nDone.")
