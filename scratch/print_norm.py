with open('ui/views/monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "def _norm_universal" in line:
        print(f"Line {i+1}: {line.strip()}")
        for j in range(i, min(i+15, len(lines))):
            print(f"{j+1}: {lines[j].rstrip()}")
