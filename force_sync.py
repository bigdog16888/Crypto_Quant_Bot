import sqlite3
import pandas as pd
import time

conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

# Set SUI / XRP trades straight to reality using direct assignment to circumvent Reconciler logic loops
try:
    cursor.execute("BEGIN TRANSACTION")
    
    # SUI (Bot 10009)
    cursor.execute("UPDATE trades SET total_invested = ?, avg_entry_price = ?, current_step = 1, basket_start_time = ? WHERE bot_id = 10009", 
                  (450.400 * 0.918249, 0.918249, int(time.time())))
    
    # XRP (Bot 10010)
    cursor.execute("UPDATE trades SET total_invested = ?, avg_entry_price = ?, current_step = 1, basket_start_time = ? WHERE bot_id = 10010", 
                  (339.300 * 1.366871, 1.366871, int(time.time())))
                  
    cursor.execute("COMMIT")
    print("Force Sync Successful.")
except Exception as e:
    cursor.execute("ROLLBACK")
    print(f"Error: {e}")

# Verify
df = pd.read_sql_query("SELECT bot_id, total_invested, avg_entry_price, current_step FROM trades WHERE bot_id IN (10009, 10010)", conn)
print(df)
conn.close()
