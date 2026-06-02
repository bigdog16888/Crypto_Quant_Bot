with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "RESET_WITH_PROOF" in line or "Found exit fill" in line:
        print(f"Line {i+1}: {line.strip()}")
        # print 50 lines around
        start = max(0, i - 25)
        end = min(len(lines), i + 25)
        for j in range(start, end):
            print(f"{j+1}: {lines[j].rstrip()}")
