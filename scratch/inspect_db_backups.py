import os
import sqlite3

backups_dir = 'backups'
if os.path.exists(backups_dir):
    files = sorted([f for f in os.listdir(backups_dir) if f.endswith('.db')])
    for fn in files:
        fp = os.path.join(backups_dir, fn)
        try:
            conn = sqlite3.connect(fp)
            cursor = conn.cursor()
            query = """
            SELECT b.id, b.name, t.open_qty, t.cycle_id, t.cycle_phase
            FROM trades t JOIN bots b ON b.id = t.bot_id
            WHERE b.normalized_pair LIKE '%SUI%'
            ORDER BY b.id;
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            if rows:
                print(f"=== DB Backup: {fn} ===")
                for row in rows:
                    print(f"  {row}")
            conn.close()
        except Exception as e:
            # Some backups might be small/corrupt/empty
            pass
else:
    print("No backups directory found")
