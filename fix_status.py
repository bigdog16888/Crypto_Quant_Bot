import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
# Update the task status to 'review' to match the review_requested run
c.execute('UPDATE tasks SET status = ? WHERE id = ?', ('review', 't_85905e6c'))
conn.commit()
print('Updated task status to review')
c.execute('SELECT id, status, current_run_id FROM tasks WHERE id = ?', ('t_85905e6c',))
print('Task after update:', c.fetchone())
conn.close()