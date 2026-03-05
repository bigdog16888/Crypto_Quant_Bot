import collections
with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = collections.deque(f, 800)

# Print ALL lines from the last ~800, filtering for anything related to BTC size, taker, or TP/grid
for line in lines:
    l = line.strip()
    low = l.lower()
    if any(k in low for k in ['taker', 'gtx', 'maker', '65524', '65000', 'tp-maintenance', 'grid-debug', 'grid-maintenance', 'amount=0.9', 'amount=0.8']):
        print(l[:250])
