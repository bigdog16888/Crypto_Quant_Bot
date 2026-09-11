import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
c.execute('SELECT * FROM task_runs WHERE task_id = ? ORDER BY id', ('t_85905e6c',))
rows = c.fetchall()
if rows:
    cols = [d[0] for d in c.description]
    print('task_runs columns:', cols)
    for r in rows:
        print(dict(zip(cols, r)))
conn.close()