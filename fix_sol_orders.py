import sqlite3, time
c = sqlite3.connect('crypto_bot.db')
c.execute(
    "UPDATE bot_orders SET status='reset_cleared', updated_at=? WHERE bot_id=10008 AND status IN ('filled','closed')",
    (int(time.time()),)
)
print('Rows marked:', c.total_changes)
c.commit()
print('Done - SOL filled/closed orders archived so MEMORY-GAP cannot re-adopt them.')
