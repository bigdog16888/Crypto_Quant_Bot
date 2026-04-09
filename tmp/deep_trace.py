import sqlite3, json, datetime

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

PROBLEM_SYMBOLS = ['LINKUSDC', 'SOLUSDC', 'SUIUSDC', 'XRPUSDC', 'BTCUSDC']

print("=== BOTS OVERVIEW (all bots for problem symbols) ===")
c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.is_active, b.status,
           t.total_invested, t.avg_entry_price, t.current_step, t.entry_confirmed, t.cycle_id
    FROM bots b LEFT JOIN trades t ON b.id=t.bot_id
    WHERE b.pair IN ('LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC','BTCUSDC')
    ORDER BY b.pair, b.id
""")
for r in c.fetchall():
    print(r)

print()
print("=== LAST 5 BOT_ORDERS FOR EACH PROBLEM BOT ===")
for sym in PROBLEM_SYMBOLS:
    c.execute("SELECT id FROM bots WHERE pair=? AND is_active=1", (sym,))
    bot_ids = [r[0] for r in c.fetchall()]
    for bid in bot_ids:
        c.execute("""
            SELECT id, bot_id, step, order_type, price, amount, filled_amount, status, 
                   client_order_id, datetime(created_at,'unixepoch','localtime') as ts, notes
            FROM bot_orders WHERE bot_id=?
            ORDER BY created_at DESC LIMIT 8
        """, (bid,))
        rows = c.fetchall()
        if rows:
            print(f"\n--- Bot {bid} ({sym}) ---")
            for r in rows:
                print(r)

print()
print("=== RECONCILIATION LOGS (last 20) ===")
c.execute("""
    SELECT datetime(timestamp,'unixepoch','localtime'), bot_id, pair, action, details
    FROM reconciliation_logs
    ORDER BY timestamp DESC LIMIT 20
""")
for r in c.fetchall():
    print(r)

print()
print("=== RESET HISTORY FROM TRADE_HISTORY (last 20 for problem symbols) ===")
c.execute("""
    SELECT datetime(timestamp,'unixepoch','localtime'), bot_id, action, symbol, price, amount, notes
    FROM trade_history
    WHERE symbol IN ('LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC','BTCUSDC')
    ORDER BY timestamp DESC LIMIT 20
""")
for r in c.fetchall():
    print(r)

conn.close()
