import sqlite3
import json
import os

DB_PATH = "crypto_bot.db"

def query_db():
    if not os.path.exists(DB_PATH):
        print(f"Error: {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    results = {}
    
    # 1. Bot details
    cursor.execute("SELECT * FROM bots WHERE id=9")
    bot = cursor.fetchone()
    if bot:
        results['bot'] = dict(bot)
    
    # 2. Trade state
    cursor.execute("SELECT * FROM trades WHERE bot_id=9")
    trade = cursor.fetchone()
    if trade:
        results['trade'] = dict(trade)
    
    # 3. Recent orders for this bot
    cursor.execute("SELECT * FROM bot_orders WHERE bot_id=9 ORDER BY id DESC LIMIT 20")
    orders = [dict(row) for row in cursor.fetchall()]
    results['orders'] = orders
    
    # 4. Active positions for the symbol
    if bot:
        pair = bot['pair'].split(':')[0].replace('/', '').upper()
        cursor.execute("SELECT * FROM active_positions WHERE pair LIKE ?", (f"%{pair}%",))
        positions = [dict(row) for row in cursor.fetchall()]
        results['positions'] = positions

    # 5. Global net discrepancy info
    cursor.execute("SELECT * FROM system_equity")
    equity = {row['key']: row['value'] for row in cursor.fetchall()}
    results['equity'] = equity

    print(json.dumps(results, indent=2))
    conn.close()

if __name__ == "__main__":
    query_db()
