import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.strategies.martingale_strategy import MartingaleStrategy

conn = get_connection()
c = conn.cursor()

# Get all in-trade bots with their TP data
c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.config,
           t.total_invested, t.avg_entry_price, t.target_tp_price, t.current_step
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    WHERE t.total_invested > 0
    ORDER BY b.id
""")
rows = c.fetchall()
conn.close()

print(f"{'ID':>6} {'Name':<20} {'Entry':>10} {'DB TP':>10} {'Entry%':>8} {'Calc TP':>10} {'tp_pct cfg':>12} {'OK?':>5}")
print("-" * 90)

for r in rows:
    bid, name, pair, direction, config_raw = r[0], r[1], r[2], r[3], r[4]
    invested, entry, db_tp, step = r[5], r[6], r[7], r[8]

    cfg = json.loads(config_raw) if config_raw else {}
    tp_pct_cfg = cfg.get('tp_pct', cfg.get('TakeProfitBase', cfg.get('TakeProfitPct', '???')))

    bot_status = {'total_invested': invested, 'avg_entry_price': entry,
                  'target_tp_price': db_tp, 'current_step': step, 'direction': direction}

    try:
        strategy = MartingaleStrategy(cfg)
        calc_tp = strategy.calculate_take_profit_price(bot_status, entry)
    except Exception as e:
        calc_tp = -1

    if entry > 0 and db_tp > 0:
        pct = (db_tp - entry) / entry * 100
    else:
        pct = 0

    ok = abs(calc_tp - db_tp) / max(db_tp, 0.0001) < 0.005 if db_tp > 0 and calc_tp > 0 else False
    flag = "OK" if ok else "WRONG"
    print(f"{bid:>6} {name:<20} {entry:>10.4f} {db_tp:>10.4f} {pct:>8.2f}% {calc_tp:>10.4f} {str(tp_pct_cfg):>12} {flag:>5}")
