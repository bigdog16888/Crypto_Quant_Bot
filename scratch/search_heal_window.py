import os

if os.path.exists('engine.log'):
    print("=== SEARCHING HEAL WINDOW LOGS ===")
    with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '2026-06-02 12:19:' in line or '2026-06-02 12:2' in line:
                if '100318' in line or 'sui long_hedge' in line:
                    if 'Executing simple maintenance path' not in line and 'REQUIRE_MANUAL_PROOF' not in line:
                        print(line.strip())
else:
    print("engine.log not found")
