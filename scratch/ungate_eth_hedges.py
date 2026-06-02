import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Ungate the three ETH hedge bots (confirmed false positive from WS fill credit lag)
bots_to_ungate = [100316, 100321, 100325]

for bot_id in bots_to_ungate:
    row = cur.execute('SELECT name, status, bot_type FROM bots WHERE id=?', (bot_id,)).fetchone()
    if row:
        print(f'  Before: id={bot_id} name={row[0]!r} status={row[1]!r} bot_type={row[2]!r}')
        cur.execute(
            "UPDATE bots SET status='Scanning' WHERE id=? AND status='REQUIRE_MANUAL_PROOF'",
            (bot_id,)
        )
        row2 = cur.execute('SELECT name, status FROM bots WHERE id=?', (bot_id,)).fetchone()
        print(f'   After: id={bot_id} name={row2[0]!r} status={row2[1]!r}')
    else:
        print(f'  Bot {bot_id} not found')

conn.commit()
conn.close()
print('Done.')
