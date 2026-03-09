import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()
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

for r in rows:
    bid, name, pair, direction, config_raw = r[0], r[1], r[2], r[3], r[4]
    cfg = json.loads(config_raw) if config_raw else {}
    entry, db_tp = r[6], r[7]
    if entry > 0 and db_tp > 0:
        pct = (db_tp - entry) / entry * 100
        if abs(pct) > 3 or db_tp == entry:
            print(f"\n=== Bot {bid}: {name} ({pair} {direction}) ===")
            print(f"  Entry: {entry}, DB TP: {db_tp}, Drift: {pct:.2f}%")
            print(f"  Config snippet:")
            for k, v in cfg.items():
                if any(x in k.lower() for x in ['tp', 'profit', 'target', 'pct', 'direction']):
                    print(f"    {k}: {v}")
