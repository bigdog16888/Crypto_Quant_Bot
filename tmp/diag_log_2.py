import re
with open('engine.log', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    
# Find the last "=== RUNNING RECONCILIATION ==="
last_recon_idx = 0
for i in range(len(lines)-1, -1, -1):
    if "=== RUNNING RECONCILIATION ===" in lines[i]:
        last_recon_idx = i
        break

with open('tmp/diag_log_output.txt', 'w', encoding='utf-8') as out:
    for line in lines[max(0, last_recon_idx-200):]:
        if 'PASS' in line or 'RESULT' in line or 'XRPUSDC' in line or 'BTCUSDC' in line or 'ETHUSDC' in line or 'XAUUSDT' in line:
            out.write(line)
