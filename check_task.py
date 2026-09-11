import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
c.execute('SELECT id, status, current_run_id FROM tasks WHERE id = ?', ('t_85905e6c',))
print('Task:', c.fetchone())
c.execute('SELECT id, status, outcome FROM runs WHERE task_id = ? ORDER BY id', ('t_85905e6c',))
for r in c.fetchall():
    print('Run:', r)
conn.close()