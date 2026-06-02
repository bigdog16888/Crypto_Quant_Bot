import os

with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'ADOPT-LIMIT-EXCEEDED' in line or 'adopt' in line.lower() or 'align' in line.lower():
            if 'info' in line.lower() or 'warn' in line.lower() or 'error' in line.lower() or 'critical' in line.lower():
                print(f"Line {i+1}: {line.strip()}")
