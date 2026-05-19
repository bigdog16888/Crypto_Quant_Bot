import sqlite3
conn = sqlite3.connect('crypto_bot.db')

print("Correct net: only status=filled exits count as live position closures")
print()

# Entry-side: all rows that opened the short (regardless of reset status —
# the exchange holds the position so they did execute)
entries = conn.execute("""
    SELECT order_type, SUM(filled_amount), status
    FROM bot_orders
    WHERE bot_id=100001 AND cycle_id=26 AND filled_amount>0
      AND order_type IN ('entry','grid')
    GROUP BY order_type, status
""").fetchall()
entry_total = 0.0
print("Entry-side (SHORT opened on exchange):")
for r in entries:
    print(f"  type={r[0]:10} status={r[2]:15} total={r[1]:.4f}")
    entry_total += r[1]
print(f"  => Total SHORT opened: {entry_total:.4f}")

print()
# Exit-side: only actual confirmed BUY fills (status=filled, non-entry-type)
# The tp row 189818234 is reset_cleared — anticipated target, not executed
exits = conn.execute("""
    SELECT order_type, SUM(filled_amount), status
    FROM bot_orders
    WHERE bot_id=100001 AND cycle_id=26 AND filled_amount>0
      AND status = 'filled'
      AND order_type NOT IN ('entry','grid')
    GROUP BY order_type, status
""").fetchall()
exit_total = 0.0
print("Exit-side (confirmed BUY fills, status=filled only):")
for r in exits:
    print(f"  type={r[0]:25} status={r[2]:15} total={r[1]:.4f}")
    exit_total += r[1]
if not exits:
    print("  (none with status=filled)")
print(f"  => Total BUY closes confirmed: {exit_total:.4f}")

print()
net = entry_total - exit_total
print(f"Net ledger position : {net:.4f} SHORT")
print(f"Exchange (100001)   : 0.4700 SHORT")
print(f"Gap                 : {abs(net - 0.47):.4f} SOL")
conn.close()
