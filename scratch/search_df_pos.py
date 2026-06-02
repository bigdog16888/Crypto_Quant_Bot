with open('ui/views/monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "df_pos_f" in line:
        print(f"Line {i+1}: {line.strip()}")
