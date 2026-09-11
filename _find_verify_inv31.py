import sqlite3, os
db_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'hermes', 'kanban', 'boards', 'studio', 'kanban.db')
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row
r = db.execute("SELECT body FROM tasks WHERE id='t_8a4394e8'").fetchone()
print(r['body'] if r else "NOT FOUND")
