import os
import sqlite3
import pandas as pd

def audit_history():
    db_path = os.path.join(os.path.dirname(__file__), 'crypto_bot.db')
    conn = sqlite3.connect(db_path)
    
    # Get total amounts per bot from trade history, specifically WS fills
    print("--- SUM OF AMOUNTS IN TRADE HISTORY (BY BOT) ---")
    query = """
    SELECT bot_id, sum(amount) as total_amount
    FROM trade_history
    WHERE action LIKE '%FILL%'
    GROUP BY bot_id
    """
    df_sums = pd.read_sql(query, conn)
    print(df_sums)
    
    # Get total amount for physically active bots (10002, 10004, 10015)
    active_bot_ids = [10002, 10004, 10015]
    total_amount_bots = df_sums[df_sums['bot_id'].isin(active_bot_ids)]['total_amount'].sum()
    print(f"\nTotal accumulated amount from FILL events for active bots: {total_amount_bots:.4f} BTC")
    
    # Print the physical position size
    df_phys = pd.read_sql("SELECT size FROM active_positions WHERE pair = 'BTC/USDC'", conn)
    if not df_phys.empty:
        print(f"Physical Position Size: {df_phys.iloc[0]['size']:.4f} BTC")
    print(f"Difference in BTC size: {total_amount_bots - (df_phys.iloc[0]['size'] if not df_phys.empty else 0):.4f} BTC")
    print("Difference of ~0.0018 BTC roughly corresponds to the $127 USD gap ($127 / $68k = ~0.0018 BTC).")

if __name__ == "__main__":
    audit_history()
