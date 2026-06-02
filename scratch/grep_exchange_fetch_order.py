with open('engine/exchange_interface.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'def fetch_order' in line:
        print(f"Line {i+1}: {line.strip()}")
