import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute("SELECT bot_id, current_step, basket_start_time FROM trades WHERE current_step > 0")
for bot_id, curr_step, basket_start in cursor.fetchall():
    print(f"\n--- BOT {bot_id} (Step {curr_step}, BasketStart {basket_start}) ---")
    query = "SELECT status, step, created_at, order_type FROM bot_orders WHERE bot_id=? AND step=?"
    df = pd.read_sql_query(query, conn, params=(bot_id, curr_step))
    print(df.to_string())
    
    # Exactly what executor runs:
    q2 = "SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status='filled' AND step=? AND created_at >= (? - 60)"
    count = cursor.execute(q2, (bot_id, curr_step, basket_start)).fetchone()[0]
    print(f"EXECUTOR QUERY RETURNS: {count}")
    
conn.close()
