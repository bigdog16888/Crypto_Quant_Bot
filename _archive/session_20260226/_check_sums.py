import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

try:
    df = pd.read_sql('SELECT bot_id, order_type, amount FROM bot_orders WHERE status="filled" AND created_at > 1771830000', conn)
    
    # Merge with bots to get pair and direction
    df_bots = pd.read_sql('SELECT id as bot_id, pair, direction FROM bots', conn)
    
    merged = df.merge(df_bots, on='bot_id', how='left')
    
    # Calculate signed amount based on direction and order_type
    # LONG entry -> +amount
    # LONG TP/SL -> -amount
    # SHORT entry -> -amount
    # SHORT TP/SL -> +amount
    def get_signed_amount(row):
        amt = row['amount']
        if row['direction'] == 'LONG':
            return amt if row['order_type'] in ['entry', 'grid'] else -amt
        else:
            return -amt if row['order_type'] in ['entry', 'grid'] else amt
            
    merged['signed_amt'] = merged.apply(get_signed_amount, axis=1)
    
    print('=== EXECUTED NET SIZES ===')
    grouped = merged.groupby(['pair', 'bot_id'])['signed_amt'].sum().reset_index()
    print(grouped)
    
    print('\n=== TOTAL PHYSICAL EXPECTED ===')
    print(merged.groupby('pair')['signed_amt'].sum().reset_index())

except Exception as e:
    print(e)
conn.close()
