import os

with open('engine/ws_event_handlers.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'Position' in line or 'position' in line.lower():
            if 'info' in line.lower() or 'warn' in line.lower() or 'error' in line.lower() or 'update' in line.lower():
                print(f"Line {i+1}: {line.strip()}")
