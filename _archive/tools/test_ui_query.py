import sqlite3
import pandas as pd
import os
import sys

# Mimic UI Logic
DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

print(f"Testing Query on: {DB_PATH}")

try:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    df = pd.read_sql("SELECT pair, side, size, entry_price FROM active_positions", conn)
    conn.close()
    
    print("\n--- DATAFRAME DUMP ---")
    print(df)
    print(f"\nRows: {len(df)}")
    
    # Mimic Calculation Logic
    net_usd = 0.0
    if not df.empty:
        for _, row in df.iterrows():
            val = row['size'] * row['entry_price']
            side = str(row['side']).upper().strip()
            if side in ['BUY', 'LONG']:
                net_usd += abs(val)
            else:
                net_usd -= abs(val)
    
    print(f"\nCalculated Net USD: {net_usd}")

except Exception as e:
    print(f"ERROR: {e}")
