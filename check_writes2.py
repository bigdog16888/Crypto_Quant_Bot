import re

with open('engine/bot_executor.py', 'r') as f:
    lines = f.readlines()

# Find all lines with conn.execute or cursor.execute or _c.execute that are NOT SELECT
pattern = re.compile(r'(conn\.execute|_c\.execute|_conn\.execute|cursor\.execute|_hc_conn\.execute|_hc_enforce_conn\.execute|_receipt_conn\.execute|_receipt_cursor\.execute)')

write_targets = ['trades', 'bot_orders', 'bots']

for i, line in enumerate(lines, 1):
    if pattern.search(line):
        # Check if it's a SELECT
        if 'SELECT' not in line.upper() and 'fetchone' not in line and 'fetchall' not in line:
            # Check if it targets trades, bot_orders, or bots
            is_write_target = any(target in line for target in write_targets)
            if is_write_target:
                print(f'{i}: {line.rstrip()}')