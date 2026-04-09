import sqlite3
c = sqlite3.connect('crypto_bot.db')
c.execute("DELETE FROM active_positions WHERE bot_id=0 AND pair='ETHUSDC' AND side='SHORT'")
c.commit()
print('Deleted stale ETHUSDC SHORT orphan row:', c.total_changes, 'row(s)')
c.close()
