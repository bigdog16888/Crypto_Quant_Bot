
import sqlite3
import pandas as pd
import os
import json

db_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

def run_diag():
    conn = sqlite3.connect(db_path)
    
    print("--- XAUUSDT INVESTIGATION ---")
    # Check bot status
    query_bots = "SELECT id, name, pair, is_active, status FROM bots WHERE pair LIKE '%XAU%'"
    bots = pd.read_sql(query_bots, conn)
    print("\nBots:")
    print(bots)
    
    for _, bot in bots.iterrows():
        bid = bot['id']
        print(f"\n--- Bot {bid} ({bot['name']}) Ledger ---")
        
        # Check trade state
        query_trade = f"SELECT * FROM trades WHERE bot_id = {bid}"
        trade = pd.read_sql(query_trade, conn)
        print("Trade State:")
        print(trade)
        
        # Check recent orders
        query_orders = f"SELECT id, order_type, order_id, price, amount, filled_amount, status, datetime(created_at, 'unixepoch', 'localtime') as time, notes FROM bot_orders WHERE bot_id = {bid} ORDER BY created_at DESC LIMIT 10"
        orders = pd.read_sql(query_orders, conn)
        print("Recent Orders:")
        print(orders)

    print("\n--- ETHUSDC INVESTIGATION ---")
    query_bots_eth = "SELECT id, name, pair, is_active, status FROM bots WHERE pair LIKE '%ETH%'"
    bots_eth = pd.read_sql(query_bots_eth, conn)
    print("\nBots:")
    print(bots_eth)
    
    for _, bot in bots_eth.iterrows():
        bid = bot['id']
        print(f"\n--- Bot {bid} ({bot['name']}) Ledger ---")
        query_trade = f"SELECT * FROM trades WHERE bot_id = {bid}"
        trade = pd.read_sql(query_trade, conn)
        print("Trade State:")
        print(trade)
        
        query_orders = f"SELECT id, order_type, order_id, price, amount, filled_amount, status, datetime(created_at, 'unixepoch', 'localtime') as time, notes FROM bot_orders WHERE bot_id = {bid} ORDER BY created_at DESC LIMIT 10"
        orders = pd.read_sql(query_orders, conn)
        print("Recent Orders:")
        print(orders)

    conn.close()

if __name__ == "__main__":
    run_diag()
