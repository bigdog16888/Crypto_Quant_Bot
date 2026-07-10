import sqlite3
import os
import time

live_db = "crypto_bot.db"

print("=== OPTIMIZING AND VERIFYING RESTORED DATABASE ===")

conn = sqlite3.connect(live_db)
try:
    # Run VACUUM to rebuild database file and clean up unused pages
    print("Running VACUUM...")
    conn.execute("VACUUM")
    conn.commit()
    print("  VACUUM complete.")
except Exception as e:
    print(f"  Error running VACUUM: {e}")
finally:
    conn.close()

# Re-check integrity
print("\nRe-checking database integrity...")
conn = sqlite3.connect(live_db)
res = conn.execute("PRAGMA integrity_check").fetchall()
print(f"  Integrity Check Result: {res}")

# Print restored details
conn.row_factory = sqlite3.Row
bots = conn.execute("SELECT id, name, pair, direction, is_active, status FROM bots ORDER BY id").fetchall()
print(f"\nRestored Bots count: {len(bots)}")
for b in bots[:15]:
    print(f"  ID: {b['id']:<6} | Name: {b['name']:<22} | Pair: {b['pair']:<15} | Dir: {b['direction']:<5} | Active: {b['is_active']} | Status: {b['status']}")
if len(bots) > 15:
    print(f"  ... and {len(bots) - 15} more bots.")

trades = conn.execute("""
    SELECT t.bot_id, b.name, t.current_step, t.total_invested, t.avg_entry_price, t.cycle_id, t.cycle_phase
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.total_invested > 0 OR t.current_step > 0
    ORDER BY t.bot_id
""").fetchall()
print(f"\nRestored Active Trades count (invested > 0 or step > 0): {len(trades)}")
for t in trades:
    print(f"  Bot: {t['bot_id']:<6} | Name: {t['name']:<22} | Step: {t['current_step']:<2} | Invested: ${t['total_invested']:<8.2f} | Avg Price: {t['avg_entry_price']:<10.4f} | Cycle: {t['cycle_id']:<4} | Phase: {t['cycle_phase']}")

print("\nRestored Row Counts:")
print("  bots:", conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0])
print("  trades:", conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
print("  bot_orders:", conn.execute("SELECT COUNT(*) FROM bot_orders").fetchone()[0])
print("  trade_history:", conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0])

conn.close()
