
import os

print("--- LIMIT CHECK OUTPUT ---")
if os.path.exists('limit_check.txt'):
    with open('limit_check.txt', 'r', encoding='utf-8', errors='ignore') as f:
        print(f.read())
else:
    print("limit_check.txt not found")

print("\n--- RECENT ENGINE ERRORS ---")
with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    f.seek(0, 2)
    size = f.tell()
    f.seek(max(0, size - 10000)) # Last 10KB
    lines = f.readlines()
    
    for line in lines:
        if 'Error' in line or 'Warning' in line or 'Failed' in line or 'BLOCK' in line:
            print(line.strip())
