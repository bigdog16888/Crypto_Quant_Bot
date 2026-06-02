import os

with open('engine/database.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'def sync_trades_from_orders' in line:
            print(f"Line {i+1}: {line.strip()}")
            break
