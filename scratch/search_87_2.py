import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bot_orders WHERE amount = 87.2 OR filled_amount = 87.2")
    print("=== BOT ORDERS WITH 87.2 ===")
    for r in cursor.fetchall():
        print(r)
        
    cursor.execute("SELECT * FROM trade_history WHERE amount = 87.2")
    print("\n=== TRADE HISTORY WITH 87.2 ===")
    for r in cursor.fetchall():
        print(r)
    conn.close()

if __name__ == '__main__':
    run()
