import os

if os.path.exists('engine.log'):
    print("=== SEARCHING engine.log ===")
    with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '2026-06-02' in line and ('SUI' in line or '100318' in line or 'OWAY_REPAIR' in line):
                if 'Executing simple maintenance path' not in line and 'REQUIRE_MANUAL_PROOF' not in line:
                    print(line.strip())
else:
    print("engine.log not found")
