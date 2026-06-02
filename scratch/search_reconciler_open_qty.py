import os

with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'open_qty =' in line or 'open_qty=' in line or 'open_qty' in line and ('UPDATE' in line or 'SET' in line):
            print(f"Line {i+1}: {line.strip()}")
