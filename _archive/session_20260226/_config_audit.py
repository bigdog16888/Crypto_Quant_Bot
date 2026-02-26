import sqlite3
import json

def get_full_configs():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    print("--- BOT CONFIGURATIONS (Recovered IDs) ---")
    cursor.execute("SELECT id, name, config, status FROM bots WHERE id BETWEEN 10006 AND 10013")
    for row in cursor.fetchall():
        print(f"BOT {row[0]} ({row[1]}) | Status: {row[3]}")
        try:
            cfg = json.loads(row[2])
            # Print specific keys to see if they were ever there
            keys = ['mode_rsi', 'rsi_level', 'mode_cci', 'cci_level', 'mode_boll']
            found = {k: cfg.get(k) for k in keys if k in cfg}
            print(f"  Triggers: {found}")
            # print(f"  Full: {row[2]}") # Too much noise?
        except:
            print(f"  INVALID JSON: {row[2]}")
    
    print("\n--- DETAILED TRADE STATE ---")
    cursor.execute("""
        SELECT b.id, b.name, b.pair, t.total_invested, t.current_step, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE t.total_invested > 0 OR b.status = 'IN TRADE'
    """)
    for row in cursor.fetchall():
        print(f"ID: {row[0]} | {row[1]} | {row[2]} | Invested: {row[3]} | Step: {row[4]} | Avg: {row[5]}")
    conn.close()

if __name__ == '__main__':
    get_full_configs()
