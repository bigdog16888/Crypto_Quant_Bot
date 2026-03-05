import sqlite3
conn = sqlite3.connect('crypto_bot.db')

print('--- TRADES TABLE ---')
trades = conn.execute('SELECT bot_id, current_step, total_invested, avg_entry_price, basket_start_time FROM trades').fetchall()
for r in trades:
    print(r)

print('\n--- BOTS (active) ---')
bots = conn.execute("SELECT id, name, pair, direction, status FROM bots WHERE status != 'Stopped'").fetchall()
for b in bots:
    print(b)

print('\n--- VIRTUAL NET PER PAIR ---')
rows = conn.execute("""
    SELECT b.pair, b.direction, SUM(t.total_invested) as invested
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    WHERE b.status != 'Stopped'
    GROUP BY b.pair, b.direction
""").fetchall()
pair_net = {}
for pair, direction, invested in rows:
    v = invested if direction == 'LONG' else -invested
    pair_net[pair] = pair_net.get(pair, 0) + v
    print(f"  {pair} {direction}: ${invested:.2f} -> contribution: ${v:.2f}")
print('\nNet per pair:')
for pair, net in pair_net.items():
    print(f"  {pair}: ${net:.2f}")
