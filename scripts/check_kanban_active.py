"""Read-only kanban board check: any tasks actively editing the CQB live tree?

Queries the ROOT kanban DB (C:/Users/Gionie/.hermes/kanban.db) in read-only
mode. No writes anywhere.
"""
import sqlite3

DB = r"C:/Users/Gionie/.hermes/kanban.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tables)

# Locate the task-like table
task_table = None
for t in tables:
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})").fetchall()]
    if "status" in cols and any(k in cols for k in ("title", "name", "prompt", "description")):
        print(f"\ntable {t} columns: {cols}")
        task_table = t
        break

if task_table:
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({task_table})").fetchall()]
    terminal = {"done", "complete", "completed", "cancelled", "canceled",
                "failed", "gave_up", "archived", "skipped"}
    rows = cur.execute(f"SELECT * FROM {task_table}").fetchall()
    print(f"\ntotal tasks: {len(rows)}")
    print("\n=== NON-TERMINAL tasks (running/queued/blocked/in-progress) ===")
    for r in rows:
        st = str(r["status"]).lower()
        if st not in terminal:
            print(f"  {r['id']}  status={r['status']}")
            for c in ("title", "name"):
                if c in r.keys() and r[c]:
                    print(f"      {c}: {str(r[c])[:140]}")
            for c in ("assignee", "worker", "owner", "profile", "updated_at", "created_at"):
                if c in r.keys() and r[c]:
                    print(f"      {c}: {r[c]}")
    print("\n=== t_d0a8526f (the worker that committed the merge + deletions) ===")
    for r in rows:
        if "d0a8526f" in str(r["id"]):
            d = dict(r)
            for k, v in d.items():
                s = str(v)
                print(f"  {k}: {s[:400]}")
else:
    print("No task-like table found — dumping all table schemas:")
    for t in tables:
        print(f"\n{t}: {[c[1] for c in cur.execute(f'PRAGMA table_info({t})').fetchall()]}")

conn.close()
