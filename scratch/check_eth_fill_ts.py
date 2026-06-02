import sqlite3, time

conn = sqlite3.connect('crypto_bot.db')
now = int(time.time())

sql = """
    SELECT b.name, bo.order_type, bo.filled_amount,
           bo.created_at, bo.updated_at, bo.filled_at, bo.status
    FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair LIKE '%ETHUSDC%'
    AND bo.filled_amount > 0
    AND bo.created_at > (strftime('%s','now') - 18000)
    ORDER BY bo.created_at DESC LIMIT 10
"""

rows = conn.execute(sql).fetchall()
for r in rows:
    name, otype, filled, created_at, updated_at, filled_at, status = r
    print(
        f"Bot: {name!r}, type: {otype}, filled: {filled}, "
        f"created_at: {created_at} ({now - created_at}s ago), "
        f"updated_at: {updated_at} ({now - updated_at}s ago), "
        f"filled_at: {filled_at} ({now - (filled_at or 0)}s ago if set), "
        f"status: {status}"
    )

conn.close()
