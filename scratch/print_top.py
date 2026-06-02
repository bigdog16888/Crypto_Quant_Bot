with open('ui/views/monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(0, min(50, len(lines))):
    print(f"{i+1}: {lines[i].rstrip()}")
