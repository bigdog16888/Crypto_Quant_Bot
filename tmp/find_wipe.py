import re
with open('engine/reconciler.py', encoding='utf-8') as f:
    lines = f.readlines()

func_start = 0
func_name = ''
func_map = {}  # start_line -> {name, refs}

for i, l in enumerate(lines):
    if re.match(r'    def |^def ', l):
        func_name = l.strip()[:80]
        func_start = i
        func_map[func_start] = {'name': func_name, 'refs': []}
    if 'safe_wipe_bot' in l:
        keys = sorted(func_map.keys(), reverse=True)
        for fs in keys:
            if i >= fs:
                is_assign = bool(re.search(r'\bsafe_wipe_bot\s*=(?!=)', l))
                is_import = 'import' in l and 'safe_wipe_bot' in l
                func_map[fs]['refs'].append((i+1, l.rstrip()[:100], is_assign, is_import))
                break

for fs, data in sorted(func_map.items()):
    if data['refs']:
        print(f"\nL{fs+1}: {data['name']}")
        for ln, text, is_assign, is_import in data['refs']:
            tag = 'ASSIGN' if is_assign else ('IMPORT' if is_import else 'call')
            print(f"  L{ln} [{tag}]: {text[:100]}")
