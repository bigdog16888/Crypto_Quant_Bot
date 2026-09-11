import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
# Release the claim lock
c.execute('UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?', ('t_85905e6c',))
conn.commit()
print('Released claim lock')
c.execute('SELECT id, status, claim_lock, claim_expires, worker_pid, current_run_id FROM tasks WHERE id = ?', ('t_85905e6c',))
print('Task after release:', c.fetchone())
conn.close()