with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for idx in range(5349, 5365):
    line = lines[idx]
    print(f"Line {idx+1}: {repr(line)}")
