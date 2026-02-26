import re, collections

counts = collections.Counter()
examples = {}

SKIP = ['Loaded BotExecutor', 'FLAG-ONLY', 'HYBRID RAW MODE', 'IN-FLIGHT']

with open('engine.log', 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        if not any(k in line for k in ['ERROR', 'WARNING', 'CRITICAL']):
            continue
        if any(s in line for s in SKIP):
            continue

        key = re.sub(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+', '', line)
        key = re.sub(r'Bot \S+', 'Bot X', key)
        key = re.sub(r'Order \d+', 'Order N', key)
        key = re.sub(r'CQB_\d+_\w+_\d+_\d+', 'CQB_X', key)
        key = re.sub(r'#\d+', '#N', key)
        key = re.sub(r'\d+\.\d+', 'N', key)
        key = re.sub(r'\d+s ago', 'Xs ago', key)
        key = key.strip()[:120]
        counts[key] += 1
        if key not in examples:
            examples[key] = line.strip()

with open('_log_analysis.txt', 'w', encoding='utf-8') as out:
    out.write('=== REMAINING WARNINGS/ERRORS (by frequency) ===\n\n')
    for key, count in counts.most_common(60):
        out.write(f'[x{count:4d}] {examples[key][:220]}\n\n')

print(f'Done. {len(counts)} distinct patterns, {sum(counts.values())} total lines.')
