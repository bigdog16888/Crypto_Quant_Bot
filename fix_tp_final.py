import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.strategies.martingale_strategy import MartingaleStrategy

conn = get_connection()
c = conn.cursor()
c.execute("""
    SELECT b.id, b.name, b.direction, b.config, t.total_invested, t.avg_entry_price, t.target_tp_price, t.current_step
    FROM bots b JOIN trades t ON b.id=t.bot_id WHERE t.total_invested>0 ORDER BY b.id
""")
rows = c.fetchall()

for r in rows:
    bid, name, direction, config_raw, invested, entry, db_tp, step = r
    cfg = json.loads(config_raw) if config_raw else {}
    bot_status = {'total_invested': invested, 'avg_entry_price': entry, 'target_tp_price': db_tp, 'current_step': step, 'direction': direction}
    strategy = MartingaleStrategy(cfg)
    new_tp = strategy.calculate_take_profit_price(bot_status, entry)
    pct = (new_tp - entry) / entry * 100 if entry > 0 else 0
    tp_type = cfg.get('TakeProfitType', 'Percent')
    tp_cfg = cfg.get('TakeProfitPct', '-') if tp_type == 'Percent' else cfg.get('TakeProfitBase', '-')
    print(f"Bot {bid:5} {name:<20} {tp_type:<8} cfg={tp_cfg:<6} entry={entry:.4f} oldTP={db_tp:.4f} newTP={new_tp:.4f} ({pct:+.2f}%)")
    c.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (new_tp, bid))

conn.commit()
conn.close()
print("\nAll TPs set to strategy-calculated values.")
