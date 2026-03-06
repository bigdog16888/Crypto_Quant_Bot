import sqlite3

def clean_duplicates():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # We will keep Bot 10015 for BTC/USDC LONG
    # We will keep Bot 10013 for ETH/USDC LONG
    # We will also keep eth (10011) which is ETH/USDC SHORT
    
    bots_to_delete = [10002, 10004, 10005, 10006, 10012]
    
    print(f"Deleting duplicate bots: {bots_to_delete}")
    
    for bot_id in bots_to_delete:
        try:
            cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))
            cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
            cursor.execute('DELETE FROM bot_orders WHERE bot_id = ?', (bot_id,))
            cursor.execute('DELETE FROM notification_log WHERE bot_id = ?', (bot_id,))
            cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
            print(f"✅ Deleted bot {bot_id} and all related records.")
        except Exception as e:
            print(f"❌ Error deleting bot {bot_id}: {e}")
            
    conn.commit()
    conn.close()
    print("Database cleaned.")

if __name__ == '__main__':
    clean_duplicates()
