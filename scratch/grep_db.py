with open('engine/database.py', 'r', encoding='utf-8', errors='ignore') as f:
    for line_num, line in enumerate(f, 1):
        if 'hedge_qty' in line:
            print(f"{line_num}: {line.strip()}")
