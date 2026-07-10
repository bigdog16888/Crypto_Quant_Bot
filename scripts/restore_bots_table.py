import sqlite3

live_db = "crypto_bot.db"
backup_db = "backups/crypto_bot.db.sui_recovery_backup"

print("=== Restoring Bots Table Verbatim from June 30 Backup ===")

conn_live = sqlite3.connect(live_db)
conn_backup = sqlite3.connect(backup_db)

c_live = conn_live.cursor()
c_backup = conn_backup.cursor()

# Get all columns and rows from backup
c_backup.execute("SELECT * FROM bots")
rows = c_backup.fetchall()

# Get column names
col_names = [description[0] for description in c_backup.description]
print(f"Columns to restore: {col_names}")
print(f"Total bots to restore: {len(rows)}")

# Clear existing bots table
c_live.execute("DELETE FROM bots")
print("Cleared existing bots table in live DB.")

# Insert verbatim
placeholders = ", ".join(["?"] * len(col_names))
sql = f"INSERT INTO bots ({', '.join(col_names)}) VALUES ({placeholders})"

inserted_count = 0
for r in rows:
    c_live.execute(sql, r)
    inserted_count += 1

conn_live.commit()
print(f"Successfully restored {inserted_count} bots to live DB.")

# Print the restored rows for confirmation
print("\n=== Restored Bots Table Rows ===")
c_live.execute("SELECT id, name, pair, direction, is_active, status FROM bots ORDER BY id")
for row in c_live.fetchall():
    print(f"  ID: {row[0]:<6} | Name: {row[1]:<25} | Pair: {row[2]:<15} | Dir: {row[3]:<5} | Active: {row[4]} | Status: {row[5]}")

conn_live.close()
conn_backup.close()
