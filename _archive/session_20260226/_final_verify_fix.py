import sqlite3
import pandas as pd
from engine.exchange_interface import ExchangeInterface

def verify():
    conn = sqlite3.connect('crypto_bot.db')
    
    print('=== DB STATUS (ETH/USDC Bots) ===')
    query = "SELECT id, name, status, config FROM bots WHERE id IN (10010, 10011, 10012, 10013)"
    df = pd.read_sql(query, conn)
    for _, row in df.iterrows():
        print(f"Bot {row['id']} ({row['name']}): Status={row['status']}")
        cfg = row['config']
        print(f"  Triggers Found: {'Yes' if 'mode_rsi' in cfg else 'No'}")

    print('\n=== DB TRADE STATE (ETH/USDC Bots) ===')
    query_t = "SELECT bot_id, total_invested, current_step FROM trades WHERE bot_id IN (10010, 10011, 10012, 10013)"
    df_t = pd.read_sql(query_t, conn)
    print(df_t)
    
    conn.close()

    print('\n=== EXCHANGE REALITY (ETH/USDC) ===')
    ex = ExchangeInterface()
    pos = ex.fetch_positions()
    found = False
    if pos:
        for p in pos:
            if 'ETH' in p['symbol']:
                print(f"Position: {p['symbol']} | Size: {p['contracts']} | Side: {p['side']}")
                found = True
    if not found:
        print("No ETH positions found on exchange. (Sync SUCCESS)")

if __name__ == '__main__':
    verify()
