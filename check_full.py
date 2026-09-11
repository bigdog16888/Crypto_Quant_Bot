import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
# Check if there are multiple boards or if the task is on a different board
c.execute('SELECT * FROM tasks WHERE id = ?', ('t_85905e6c',))
row = c.fetchone()
cols = [d[0] for d in c.description]
print('Full task row:', dict(zip(cols, row)))
conn.close()