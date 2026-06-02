with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "_find_proof_of_exit" in line and "def " not in line:
        print(f"Line {i+1}: {line.strip()}")
        # print 5 lines around
        for j in range(max(0, i-2), min(len(lines), i+3)):
            print(f"{j+1}: {lines[j].rstrip()}")
