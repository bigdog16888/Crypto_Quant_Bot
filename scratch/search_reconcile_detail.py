import os

if os.path.exists('engine.log'):
    with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '2026-06-02 12:' in line and ('100318' in line or 'sui long_hedge' in line or 'SUI' in line):
                if 'Executing simple maintenance path' not in line and 'REQUIRE_MANUAL_PROOF' not in line:
                    print(line.strip())
