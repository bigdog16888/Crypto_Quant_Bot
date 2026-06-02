with open('engine/database.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

found = False
for i, line in enumerate(lines):
    if "def get_pair_virtual_net" in line:
        found = True
        print(f"Line {i+1}: {line.strip()}")
        for j in range(i, min(i+40, len(lines))):
            print(f"{j+1}: {lines[j].rstrip()}")
        break
