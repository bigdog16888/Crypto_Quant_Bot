from engine.database import get_connection
c = get_connection().cursor()

# See bots schema
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("Tables:", [r[0] for r in c.fetchall()])

c.execute("PRAGMA table_info(bots)")
print("\nBots columns:", [r[1] for r in c.fetchall()])

# Fetch ETH bot
c.execute("SELECT * FROM bots WHERE pair LIKE '%ETH%' LIMIT 5")
cols = [d[0] for d in c.description]
rows = [dict(zip(cols, r)) for r in c.fetchall()]
print("\nETH bots:", rows)

# Fetch LINK bot
c.execute("SELECT * FROM bots WHERE pair LIKE '%LINK%' LIMIT 5")
cols = [d[0] for d in c.description]
rows = [dict(zip(cols, r)) for r in c.fetchall()]
print("\nLINK bots:", rows)
