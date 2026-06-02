import os

if os.path.exists('engine.log'):
    print("=== SEARCHING engine.log 12:28:50 - 12:29:30 ===")
    with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '2026-06-02 12:29:' in line:
                ts_part = line.split()[1] # e.g. "12:29:05,123"
                sec = int(ts_part.split(',')[0].split(':')[2])
                if sec <= 30:
                    if '100318' in line or 'sui long_hedge' in line or 'SUI' in line:
                        print(line.strip())
else:
    print("engine.log not found")
