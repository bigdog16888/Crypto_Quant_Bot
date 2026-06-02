with open('ui/views/monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

found = False
for i, line in enumerate(lines):
    if "def _fetch_fresh_monitor_data" in line:
        found = True
        print(f"Line {i+1}: {line.strip()}")
        for j in range(i, min(i+100, len(lines))):
            print(f"{j+1}: {lines[j].rstrip()}")
        break

if not found:
    print("Function not found!")
