with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'def reconcile_all' in line:
        print(f"Line {i+1}: {line.strip()}")
