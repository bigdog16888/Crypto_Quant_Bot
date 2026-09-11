import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
# Check if there's a specific status needed for completion
c.execute('SELECT DISTINCT status FROM tasks')
print('All task statuses:', [r[0] for r in c.fetchall()])
# Check the current task status
c.execute('SELECT id, status, current_run_id FROM tasks WHERE id = ?', ('t_85905e6c',))
print('Current task:', c.fetchone())
conn.close()