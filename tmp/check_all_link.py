import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT o.bot_id, SUM(o.filled_amount) FROM bot_orders o JOIN bots b ON o.bot_id = b.id WHERE b.pair='LINKUSDC' AND o.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') GROUP BY o.bot_id")
for r in q.fetchall(): print(r)
print("----")
q.execute("SELECT o.bot_id, SUM(o.filled_amount) FROM bot_orders o JOIN bots b ON o.bot_id = b.id WHERE b.pair='LINKUSDC' AND o.order_type IN ('tp', 'exit', 'sl', 'adoption_reduce', 'dust_close') GROUP BY o.bot_id")
for r in q.fetchall(): print(r)
c.close()
