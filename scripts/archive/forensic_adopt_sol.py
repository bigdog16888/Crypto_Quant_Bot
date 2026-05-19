import sqlite3
import sys
import time

conn = sqlite3.connect('crypto_bot.db')

# Guard: abort if rows already exist
existing = conn.execute(
    "SELECT COUNT(*) FROM bot_orders WHERE order_id IN ('189751954','189763240')"
).fetchone()[0]
if existing > 0:
    print(f"ABORT: {existing} row(s) already exist. Nothing written.")
    conn.close()
    sys.exit(1)

now = int(time.time())

conn.execute("""
INSERT INTO bot_orders (
    bot_id, order_type, order_id, client_order_id,
    price, amount, filled_amount, status,
    cycle_id, created_at, updated_at,
    wipe_proof_source, notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    100001, 'anonymous_adopt', '189751954', 'CQB_FORENSIC_189751954',
    90.86, 0.11, 0.11, 'filled',
    26, 1778736780, now,
    'forensic_adopt',
    'Forensic adoption: anonymous BUY fill. Non-CQB CID 4Q91OsBdFYdBszANfzw9yf. ANONYMOUS-ADOPT failed due to symbol mismatch bug.'
))

conn.execute("""
INSERT INTO bot_orders (
    bot_id, order_type, order_id, client_order_id,
    price, amount, filled_amount, status,
    cycle_id, created_at, updated_at,
    wipe_proof_source, notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    100001, 'anonymous_adopt', '189763240', 'CQB_FORENSIC_189763240',
    91.30, 0.11, 0.11, 'filled',
    26, 1778738042, now,
    'forensic_adopt',
    'Forensic adoption: anonymous BUY fill. Non-CQB CID xvdSjwAEPaiKgXrj3jGUBH. ANONYMOUS-ADOPT failed due to symbol mismatch bug.'
))

conn.commit()

# Verify insertion
print("Inserted rows:")
rows = conn.execute("""
    SELECT order_id, order_type, filled_amount, price, status, cycle_id, wipe_proof_source
    FROM bot_orders
    WHERE order_id IN ('189751954', '189763240')
""").fetchall()
for r in rows:
    print(f"  {r}")

# Net accounting after adoption
print("\nBot 100001 cycle 26 — full accounting after adoption:")
rows2 = conn.execute("""
    SELECT order_type, SUM(filled_amount) as total, COUNT(*) as cnt,
           GROUP_CONCAT(status) as statuses
    FROM bot_orders
    WHERE bot_id = 100001
      AND cycle_id = 26
      AND filled_amount > 0
      AND status NOT IN ('cancelled','canceled','failed','open','new','rejected')
    GROUP BY order_type
    ORDER BY order_type
""").fetchall()

entry_total = 0.0
exit_total  = 0.0
for r in rows2:
    otype, total, cnt, statuses = r
    print(f"  type={otype:30} | total={total:.4f} | rows={cnt}")
    if otype in ('entry', 'grid'):
        entry_total += total
    elif 'tp' in otype or 'adopt' in otype:
        exit_total += total

print(f"\n  Entry-side (SHORT opened) : {entry_total:.4f}")
print(f"  Exit-side  (BUY closes)   : {exit_total:.4f}")
print(f"  Net claimed by ledger     : {entry_total - exit_total:.4f} SHORT")
print(f"  Exchange holds (100001)   : 0.4700 SHORT")
print(f"  Gap after adoption        : {abs(entry_total - exit_total - 0.47):.4f} SOL")

conn.close()
