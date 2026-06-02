import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # Query all bots that have parent_bot_id or hedge_child_bot_id, or are hedge_child
    cursor.execute("""
        SELECT id, name, pair, direction, is_active, status, bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step
        FROM bots
    """)
    print("=== All Bots in DB ===")
    for row in cursor.fetchall():
        print(row)
        
    conn.close()

if __name__ == '__main__':
    run()
