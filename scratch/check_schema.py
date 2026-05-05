from engine.database import get_connection
import pandas as pd

conn = get_connection()
print("--- bot_orders schema ---")
print(pd.read_sql("PRAGMA table_info(bot_orders);", conn))

print("\n--- trades schema ---")
print(pd.read_sql("PRAGMA table_info(trades);", conn))
