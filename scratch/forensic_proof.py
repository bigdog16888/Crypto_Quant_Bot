import sys
import os
import pandas as pd
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection

def forensic_audit(symbol_exchange, symbol_db_search):
    ex = ExchangeInterface()
    conn = get_connection()
    
    print(f"--- Forensic Audit for {symbol_exchange} ---")
    
    # 1. Fetch Exchange Trades
    print("Fetching exchange trades...")
    exchange_trades = ex.fetch_my_trades(symbol_exchange, limit=500)
    df_ex = pd.DataFrame(exchange_trades)
    if df_ex.empty:
        print("No exchange trades found.")
        return
    
    # 2. Fetch DB Orders
    print("Fetching database orders...")
    query = "SELECT * FROM bot_orders WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE ?)"
    df_db = pd.read_sql(query, conn, params=(f"%{symbol_db_search}%",))
    
    # 3. Identify Orphans (Exchange Trades NOT in DB)
    # We match by order_id (string in DB, int in exchange)
    db_order_ids = set(df_db['order_id'].astype(str).tolist())
    
    orphans = []
    matches = 0
    for _, trade in df_ex.iterrows():
        oid = str(trade['orderId'])
        if oid in db_order_ids:
            matches += 1
        else:
            orphans.append(trade)
    
    df_orphans = pd.DataFrame(orphans)
    
    print(f"Total Exchange Trades: {len(df_ex)}")
    print(f"Matched in DB: {matches}")
    print(f"Orphans (Missing in DB): {len(df_orphans)}")
    
    if not df_orphans.empty:
        # Sum Orphan Quantities (Buy - Sell)
        # In CCXT: 'side' is 'buy' or 'sell'
        df_orphans['qty_net'] = df_orphans.apply(lambda x: x['amount'] if x['side'] == 'buy' else -x['amount'], axis=1)
        orphan_net = df_orphans['qty_net'].sum()
        print(f"\nNet Orphan Quantity: {orphan_net:.4f}")
        
        print("\nTop 10 Orphans:")
        print(df_orphans[['id', 'side', 'amount', 'price', 'timestamp']].head(10))
    else:
        print("\nNo orphans found.")

if __name__ == "__main__":
    # symbol_exchange, symbol_db_search
    # BTC
    # forensic_audit('BTC/USDC:USDC', 'BTC')
    # SUI
    forensic_audit('SUI/USDC:USDC', 'SUI')
