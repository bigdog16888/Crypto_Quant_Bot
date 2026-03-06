import sqlite3, json, time
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT b.id, b.name, t.total_invested, t.avg_entry_price, t.target_tp_price, b.config, t.basket_start_time FROM bots b JOIN trades t ON b.id=t.bot_id WHERE t.total_invested > 0')
bots = c.fetchall()
now = time.time()
for b in bots:
    bid, name, inv, entry, tp, cfg, start = b
    cfg_json = json.loads(cfg)
    is_ee = cfg_json.get("UseEarlyExit", False)
    age = (now - start) / 3600
    print(f"Bot {bid} ({name}) | Age: {age:.2f}h | UseEE: {is_ee} | Inv: ${inv:.2f} | Entry: {entry:.4f} | TP: {tp:.4f}")
conn.close()
