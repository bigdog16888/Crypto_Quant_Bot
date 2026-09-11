import sqlite3
conn = sqlite3.connect('C:/Users/Gionie/AppData/Local/hermes/kanban/boards/studio/kanban.db')
c = conn.cursor()
# Check all boards/tables to see if there are multiple databases
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%board%'")
print('Board-related tables:', c.fetchall())
# Check the kanban_notify_subs
c.execute('SELECT * FROM kanban_notify_subs')
print('Notify subs:', c.fetchall())
# Check task_links for parent-child
c.execute('SELECT * FROM task_links WHERE child_id = ? OR parent_id = ?', ('t_85905e6c', 't_85905e6c'))
print('Task links:', c.fetchall())
# Check if there's a tenant field
c.execute('SELECT id, tenant FROM tasks WHERE id = ?', ('t_85905e6c',))
print('Task tenant:', c.fetchall())
conn.close()