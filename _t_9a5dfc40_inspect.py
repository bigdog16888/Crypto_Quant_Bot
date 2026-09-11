"""Read-only inspection for t_9a5dfc40: check bots rows + leftover test rows + schema."""
import sqlite3

conn = sqlite3.connect("crypto_bot.db")
print("bots 100317/200000:", conn.execute(
    "SELECT id, pair, status FROM bots WHERE id IN (100317, 200000)").fetchall())
print("leftover test rows:", conn.execute(
    "SELECT id, bot_id, client_order_id, status FROM bot_orders "
    "WHERE client_order_id IN ('TEST_ORDER','FAKE_ORDER','CONC')").fetchall())
bots_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='bots'").fetchone()
print("bots schema:", bots_sql[0][:600] if bots_sql else None)
bo_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='bot_orders'").fetchone()
print("bot_orders schema:", bo_sql[0][:800] if bo_sql else None)
conn.close()
