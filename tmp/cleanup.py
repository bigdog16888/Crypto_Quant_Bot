"""Cleanup stranded BTC orders from before cycle 33."""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
cursor = conn.cursor()
cursor.execute("UPDATE bot_orders SET status='cancelled' WHERE status IN ('open', 'new', 'placing') AND bot_id=10016 AND cycle_id < 33")
conn.commit()
print("Cleaned up orphaned BTC orders.")
conn.close()
