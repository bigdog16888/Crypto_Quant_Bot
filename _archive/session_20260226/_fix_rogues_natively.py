import sqlite3
import time

def auto_adopt_rogues():
    from engine.database import import_position_from_exchange, get_connection
    print("🚀 AUTO-ADOPTING ROGUE POSITIONS NATIVELY...")
    
    # 1. 0.519 ETH to Bot 10013
    print("Adopting 0.519 ETH to Bot 10013...")
    success1 = import_position_from_exchange(
        bot_id=10013, 
        pair='ETH/USDC:USDC', 
        position_size=0.519, 
        entry_price=1883.63, 
        direction='LONG'
    )
    print(f"Result: {success1}")

    # 2. 0.027 BTC to Bot 10012
    # The reality is Exchange has 0.298 BTC. Bots 10002, 10004, 10015 claim 0.271 BTC total.
    # The missing 0.027 BTC belongs to 10012.
    print("Adopting 0.027 BTC to Bot 10012...")
    success2 = import_position_from_exchange(
        bot_id=10012, 
        pair='BTC/USDC:USDC', 
        position_size=0.027, 
        entry_price=65568.60, 
        direction='LONG'
    )
    print(f"Result: {success2}")
    
    # 3. Clean up the zombie DB state for bot 10013 from the manual script that Reconciler didn't fully wipe
    # (Just in case the Status is stuck on Scanning despite having trades invested)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id IN (10012, 10013)")
    conn.commit()
    conn.close()
    
    print("✅ Adoption Script Finished.")

if __name__ == "__main__":
    auto_adopt_rogues()
