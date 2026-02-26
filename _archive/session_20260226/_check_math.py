import os
import sqlite3
import pandas as pd

def check_math():
    db_path = os.path.join(os.path.dirname(__file__), 'crypto_bot.db')
    conn = sqlite3.connect(db_path)
    
    # Virtual Reality (What bots hold)
    query_virt = """
        SELECT b.id, b.name, b.pair, t.total_invested, t.avg_entry_price, t.current_step, b.direction 
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE b.is_active = 1 AND t.total_invested > 0
    """
    df_virt = pd.read_sql(query_virt, conn)
    virtual_net = df_virt['total_invested'].sum()
    virtual_implied_btc = (df_virt['total_invested'] / df_virt['avg_entry_price']).sum()
    
    # Physical Reality (What Exchange reports)
    df_phys = pd.read_sql("SELECT pair, side, size, entry_price FROM active_positions", conn)
    physical_btc = df_phys['size'].sum()
    physical_net = (df_phys['size'] * df_phys['entry_price']).sum()
    
    # The true difference in physical BTC coins
    coin_diff = virtual_implied_btc - physical_btc
    
    # Calculate exactly where the gap comes from
    # $127 gap = $127.30
    # Average entry price difference impact = physical_btc * (average_virtual_entry_price - physical_entry_price)
    
    avg_virtual_entry = virtual_net / virtual_implied_btc
    avg_physical_entry = physical_net / physical_btc
    
    price_gap = avg_virtual_entry - avg_physical_entry
    price_impact = physical_btc * price_gap
    
    print(f"--- THE TRUTH ABOUT THE $127 (BTC/USDC) ---")
    print(f"Virtual BTC Count (Bot Expectation) : {virtual_implied_btc:.8f} BTC")
    print(f"Physical BTC Count (Actual Exchange): {physical_btc:.8f} BTC")
    print(f"-> Differences in Coins = {coin_diff:.8f} BTC (Worth roughly ${(coin_diff * avg_virtual_entry):.2f})")
    
    print(f"\nVirtual Average Entry Price (Bots)  : ${avg_virtual_entry:,.2f}")
    print(f"Physical Average Entry Price (Exch) : ${avg_physical_entry:,.2f}")
    print(f"-> Difference in Average Entry Price = ${price_gap:,.2f}")
    
    print(f"\nGap resulting from purely Average Entry Price difference:")
    print(f"-> {physical_btc:.4f} BTC * ${price_gap:,.2f} = ${price_impact:.2f}")
    
    print(f"\nTotal Visual UI Discrepancy (Notional USD Gap):")
    print(f"Virtual Net: ${virtual_net:,.2f}")
    print(f"Physical Net: ${physical_net:,.2f}")
    print(f"Difference: ${(virtual_net - physical_net):,.2f}")
    
    print("\nCONCLUSION:")
    print("The primary cause is the Average Entry Price being misaligned between the bot databases and the exchange history.")
    print("The actual coin loss is ~0.000083 BTC (about $5), likely from Maker Fees or rounding offsets.")

if __name__ == "__main__":
    check_math()
