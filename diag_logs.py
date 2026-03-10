import os

log_files = [
    r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine.log',
    r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine.log.1'
]

out_lines = []
for lf in log_files:
    if not os.path.exists(lf): continue
    out_lines.append(f"\n=== SCANNING {os.path.basename(lf)} ===")
    try:
        with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '[RECON' in line or 'ORPHAN' in line or 'POSITION-SYNC' in line or 'SYNC-TO-REALITY' in line or 'GHOST' in line:
                    if 'LINK' in line.upper() or '10020' in line:
                        out_lines.append(line.strip())
    except Exception as e:
        out_lines.append(f"Error reading log {lf}: {e}")

with open('diag_clean.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out_lines))
