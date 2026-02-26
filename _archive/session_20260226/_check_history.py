import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
pd.set_option('display.max_columns', None)
pd.set_option('display.max_colwidth', None)
# Let's see recent trade history involving 10012 or 10013
query = "SELECT datetime(timestamp, 'unixepoch', 'localtime') as time, action, symbol, price, amount, notes FROM trade_history WHERE notes LIKE '%10013%' OR notes LIKE '%10012%' ORDER BY timestamp DESC LIMIT 20"
print("=== TRADE HISTORY (10012, 10013) ===")
print(pd.read_sql_query(query, conn))

# Let's also see recent reconciliation_logs
query2 = "SELECT datetime(timestamp, 'unixepoch', 'localtime') as time, action, details FROM reconciliation_logs WHERE details LIKE '%10013%' OR details LIKE '%10012%' ORDER BY timestamp DESC LIMIT 20"
print("\n=== RECONCILIATION LOGS (10012, 10013) ===")
print(pd.read_sql_query(query2, conn))
conn.close()
