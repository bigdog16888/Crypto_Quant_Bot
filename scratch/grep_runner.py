with open('engine/runner.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'reconciler' in line or 'StateReconciler' in line:
        print(f"Line {i+1}: {line.strip()}")
