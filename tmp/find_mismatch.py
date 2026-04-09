import sys
with open('engine.log', 'r', encoding='utf-8') as f:
    for line in f.readlines()[-2000:]:
        if 'mismatch' in line and 'eth' in line.lower():
            print(line.strip())
