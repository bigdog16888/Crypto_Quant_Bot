import sqlite3
import datetime

def check_sui_orders():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    print("--- ALL SUI BOT ORDERS TODAY (2026-05-27) ---")
    cur.execute("""
        SELECT id, bot_id, order_type, order_id, price, amount, status, created_at 
        FROM bot_orders 
        WHERE (bot_id IN (10018, 100000) OR notes LIKE '%SUI%') 
          AND created_at >= 1779801600
        ORDER BY created_at ASC
    """)
    rows = cur.fetchall()
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r[7])
        print(f"ID: {r[0]} | Bot: {r[1]} | Type: {r[2]} | OID: {r[3]} | Px: {r[4]} | Amt: {r[5]} | Status: {r[6]} | Created: {dt}")
        
    print("\n--- ALL SUI TRADE HISTORY TODAY (2026-05-27) ---")
    cur.execute("""
        SELECT id, bot_id, action, symbol, price, amount, timestamp 
        FROM trade_history 
        WHERE (bot_id IN (10018, 100000) OR symbol LIKE '%SUI%')
          AND timestamp >= 1779801600
        ORDER BY timestamp ASC
    """)
    rows = cur.fetchall()
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r[6])
        print(f"ID: {r[0]} | Bot: {r[1]} | Act: {r[2]} | Sym: {r[3]} | Px: {r[4]} | Amt: {r[5]} | Time: {dt}")
        
    conn.close()

if __name__ == '__main__':
    check_sui_orders()
