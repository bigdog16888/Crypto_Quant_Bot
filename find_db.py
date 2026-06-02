import sqlite3

for dbfile in ['CQB.db', 'crypto_bot.db', 'bot_data.db']:
    try:
        db = sqlite3.connect(dbfile)
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        print(f"{dbfile}: {tables}")
        db.close()
    except Exception as e:
        print(f"{dbfile}: ERROR {e}")
