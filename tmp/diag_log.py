import re
with open('engine.log', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    
# Find the last "=== RUNNING RECONCILIATION ==="
last_recon_idx = 0
for i in range(len(lines)-1, -1, -1):
    if "=== RUNNING RECONCILIATION ===" in lines[i]:
        last_recon_idx = i
        break

print(f"Showing logs from line {last_recon_idx} onwards for specific keywords:")
for line in lines[last_recon_idx:]:
    if 'PASS' in line or 'RESULT' in line or 'XRPUSDC' in line or 'BTCUSDC' in line or 'ETHUSDC' in line:
        print(line.strip())
