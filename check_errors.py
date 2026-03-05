import collections
with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = collections.deque(f, 500)

errors = []
warns = []
for line in lines:
    l = line.strip()
    if 'ERROR' in l or 'CRITICAL' in l:
        errors.append(l[:220])
    elif 'WARNING' in l and 'HYBRID RAW' not in l:
        warns.append(l[:220])

print(f"=== ERRORS ({len(errors)}) ===")
for e in errors[-30:]:
    print(e)
print(f"\n=== WARNINGS ({len(warns)}) ===")
for w in warns[-30:]:
    print(w)
