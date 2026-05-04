import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

bots = {
    10007: ('BNBUSDC', 'SHORT'),
    10011: ('ETHUSDC', 'SHORT'),
    10016: ('BTCUSDC', 'LONG'),
    10017: ('XRPUSDC', 'LONG'),
    10018: ('SUIUSDC', 'LONG'),
    10019: ('XAUUSDT', 'SHORT'),
    100000: ('SUIUSDC', 'SHORT'),
}
print("Bot   | Name                 | open_qty | physical | parity")
print("-" * 65)
for bot_id, (pair, side) in bots.items():
    cur.execute('SELECT open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id=?', (bot_id,))
    t = cur.fetchone()
    cur.execute('SELECT b.name FROM bots b WHERE b.id=?', (bot_id,))
    nb = cur.fetchone()
    cur.execute('SELECT size FROM active_positions WHERE pair=? AND side=?', (pair, side))
    p = cur.fetchone()
    oq = float(t['open_qty'] or 0) if t else 0.0
    ps = float(p['size'] or 0) if p else 0.0
    name = nb['name'] if nb else '?'
    diff = abs(oq - ps)
    status = "OK" if diff < 0.0001 else f"MISMATCH +{diff:.6f}"
    print(f"  {bot_id:6d} | {name:20s} | {oq:8.4f} | {ps:8.4f} | {status}")

print()
print("If all OK: ledger is 1:1 with exchange.")
conn.close()
