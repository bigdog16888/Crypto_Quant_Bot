import sqlite3

def start_bots():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    # Turn on 10012 (BTC) and 10013 (ETH)
    cursor.execute("UPDATE bots SET status='Scanning' WHERE id IN (10012, 10013)")
    conn.commit()
    conn.close()
    print("Bots 10012 and 10013 set to Scanning.")

if __name__ == "__main__":
    start_bots()
