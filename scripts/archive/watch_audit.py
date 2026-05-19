"""
watch_audit.py — Monitor exchange_order_audit for new rows.
Run from project root: python watch_audit.py

Prints any new row the moment it appears.
Press Ctrl+C to stop.
"""
import sqlite3
import time

DB_PATH = "crypto_bot.db"
POLL_INTERVAL = 5  # seconds


def main():
    last_id = 0
    print(f"[watch_audit] Monitoring exchange_order_audit in {DB_PATH}")
    print(f"[watch_audit] Poll interval: {POLL_INTERVAL}s — Ctrl+C to stop\n")

    # Show current count so we know starting state
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT COUNT(*) FROM exchange_order_audit").fetchone()
    conn.close()
    print(f"[watch_audit] Table exists. Current row count: {row[0]}")
    print(f"[watch_audit] Waiting for emergency healing events...\n")

    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM exchange_order_audit WHERE id > ? ORDER BY id ASC",
                (last_id,)
            )
            rows = cursor.fetchall()
            for row in rows:
                last_id = row["id"]
                ts = time.strftime('%H:%M:%S')
                print(f"\n[{ts}] ══ NEW AUDIT ROW ══════════════════════════════")
                print(f"  id          : {row['id']}")
                print(f"  order_id    : {row['order_id']}")
                print(f"  client_oid  : {row['client_order_id']}")
                print(f"  symbol      : {row['symbol']}")
                print(f"  side        : {row['side']}")
                print(f"  qty         : {row['qty']}")
                print(f"  price       : {row['price']}")
                print(f"  context     : {row['context']}")
                print(f"  call_site   : {row['call_site']}")
                print(f"  bot_id      : {row['bot_id']}")
                print(f"  cycle_id    : {row['cycle_id']}")
                placed = row['placed_at']
                placed_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(placed)) if placed else 'N/A'
                print(f"  placed_at   : {placed} ({placed_str})")
                print(f"  notes       : {row['notes']}")
                print(f"  ─────────────────────────────────────────────────")
            conn.close()
        except KeyboardInterrupt:
            print("\n[watch_audit] Stopped.")
            break
        except Exception as e:
            print(f"[watch_audit] Error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
