import sqlite3, os

db_path = os.path.expanduser("~/.hermes/kanban.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT * FROM tasks WHERE id='t_ce32001b'")
rows = cur.fetchall()
if not rows:
    print("NOT FOUND")
else:
    r = rows[0]
    for k in r.keys():
        v = r[k]
        if v is None:
            continue
        s = str(v)
        if len(s) > 5000:
            s = s[:5000] + f"... [TRUNCATED total {len(str(v))} chars]"
        print(f"--- {k} ---")
        print(s)

print("\n\n== comments ==")
cur.execute("SELECT * FROM comments WHERE task_id='t_ce32001b' ORDER BY created_at")
for r in cur.fetchall():
    body = str(r["body"]) if "body" in r.keys() else str(dict(r))
    if len(body) > 3000:
        body = body[:3000] + f"... [TRUNCATED total {len(body)} chars]"
    print("----")
    print(body)

print("\n\n== events ==")
cur.execute("SELECT * FROM events WHERE task_id='t_ce32001b' ORDER BY created_at")
for r in cur.fetchall():
    d = dict(r)
    s = str(d)
    if len(s) > 2000:
        s = s[:2000] + f"... [TRUNCATED total {len(s)} chars]"
    print(s)
conn.close()
