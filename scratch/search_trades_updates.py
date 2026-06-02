import os

log_files = ['engine.log'] + [f'engine.log.{i}' for i in range(1, 6)]
for lf in log_files:
    if os.path.exists(lf):
        try:
            print(f"=== Searching {lf} ===")
            with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if 'UPDATE trades' in line or 'trades SET' in line:
                        if '100318' in line or '10018' in line:
                            print(line.strip())
        except Exception as e:
            print(f"Error reading {lf}: {e}")
