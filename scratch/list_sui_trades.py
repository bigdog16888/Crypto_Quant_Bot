from engine.database import get_connection

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT b.id, b.pair, t.hedge_qty, t.open_qty, b.direction FROM trades t JOIN bots b ON t.bot_id=b.id WHERE b.pair LIKE '%SUI%'")
rows = cur.fetchall()
print("SUI BOTS IN TRADES TABLE:")
for r in rows:
    print(f"ID: {r[0]} | Pair: {r[1]} | Hedge: {r[2]} | Open: {r[3]} | Dir: {r[4]}")
