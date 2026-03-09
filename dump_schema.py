import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in c.fetchall()]
for t in tables:
    c.execute(f"PRAGMA table_info({t})")
    cols = c.fetchall()
    print(f"TABLE: {t}")
    for col in cols:
        pk = " PK" if col[5] else ""
        nn = " NOT NULL" if col[3] else ""
        default = f" DEFAULT {col[4]}" if col[4] is not None else ""
        print(f"  {col[1]:30s} {col[2]:15s}{pk}{nn}{default}")
    c.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"  [rows: {c.fetchone()[0]}]")
    print()
conn.close()
