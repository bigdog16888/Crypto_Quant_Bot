import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('Tables:', c.fetchall())
c.execute('SELECT * FROM tasks WHERE id = ?', ('t_85905e6c',))
row = c.fetchone()
if row:
    cols = [d[0] for d in c.description]
    print('Task columns:', cols)
    print('Task:', dict(zip(cols, row)))
conn.close()