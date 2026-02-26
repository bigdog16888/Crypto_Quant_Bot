import sqlite3
import time

def rescue():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # 🚀 SMART ADOPTION: Recovering Ghost Positions
    # These were "lost" during the Reconciler reset bug.
    # We reconstruct the virtual state from physical reality.

    # 1. BOT 10012 (BTC/USDC)
    # Physical Excess: 0.027 BTC (~$1772.71)
    # Reality: 0.096 total on exchange. Bots 10002+10004+10015 claim 0.069.
    # 0.096 - 0.069 = 0.027.
    btc_unclaimed_qty = 0.027
    btc_entry = 65656.63
    btc_invested = round(btc_unclaimed_qty * btc_entry, 2)
    
    print(f"Adopting {btc_unclaimed_qty} BTC for Bot 10012 (Invested: ${btc_invested})")
    cursor.execute("""
        UPDATE trades 
        SET total_invested=?, current_step=?, avg_entry_price=?, 
            entry_confirmed=1, basket_start_time=?
        WHERE bot_id=10012
    """, (btc_invested, 2, btc_entry, int(time.time())))
    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10012")

    # 2. BOT 10013 (ETH/USDC)
    # Physical Excess: 0.519 ETH (~$977.60)
    eth_unclaimed_qty = 0.519
    eth_entry = 1883.63
    eth_invested = round(eth_unclaimed_qty * eth_entry, 2)

    print(f"Adopting {eth_unclaimed_qty} ETH for Bot 10013 (Invested: ${eth_invested})")
    cursor.execute("""
        UPDATE trades 
        SET total_invested=?, current_step=?, avg_entry_price=?, 
            entry_confirmed=1, basket_start_time=?
        WHERE bot_id=10013
    """, (eth_invested, 2, eth_entry, int(time.time())))
    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=10013")

    conn.commit()
    conn.close()
    print("✅ Smart Adoption Complete. Virtual positions now match Exchange reality.")

if __name__ == '__main__':
    rescue()
