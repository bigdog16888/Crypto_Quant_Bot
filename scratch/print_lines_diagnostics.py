with open('ui/views/monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(735, min(765, len(lines))):
    print(f"{i+1}: {lines[i].rstrip()}")
