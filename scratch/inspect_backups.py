import sqlite3
import os

def run():
    # Let's inspect backups to see order cycle history for bot 100317
    backup_dir = 'backups'
    db_files = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')]
    db_files.append('crypto_bot_backup_before_stale_hedges.db')
    db_files.append('crypto_bot.db')
    
    for db_path in sorted(db_files):
        if not os.path.exists(db_path) or os.path.getsize(db_path) < 10000:
            continue
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            rows = cursor.execute("SELECT cycle_id, order_type, status, amount, price, client_order_id, created_at FROM bot_orders WHERE bot_id = 100317 ORDER BY created_at ASC").fetchall()
            if rows:
                print(f"\n========================================\nDatabase: {db_path}")
                for r in rows:
                    print(r)
        except Exception as e:
            print(f"Error reading {db_path}: {e}")

if __name__ == '__main__':
    run()
