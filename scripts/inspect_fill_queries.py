with open('engine/position_ledger.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

print('=== ALL exchange_fills QUERY LOCATIONS IN position_ledger.py ===')
for i, l in enumerate(lines):
    if 'exchange_fills' in l.upper() and ('FROM' in l.upper() or 'SELECT' in l.upper()):
        print(f'\n--- Fill query candidate at line {i+1} ---')
        start = max(0, i - 3)
        end = min(len(lines), i + 15)
        for j in range(start, end):
            print(f'{j+1:>4}: {lines[j]}', end='')
