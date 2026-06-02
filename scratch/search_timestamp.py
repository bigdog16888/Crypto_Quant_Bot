import os

def search_timestamp():
    log_files = [f for f in os.listdir('.') if f.startswith('engine.log')]
    log_files.sort(key=lambda x: int(x.split('.')[-1]) if x.split('.')[-1].isdigit() else 0)
    
    target = "2026-05-27 13:54:38"
    for f in log_files:
        print(f"Checking {f}...")
        lines = []
        try:
            with open(f, 'r', encoding='utf-8', errors='ignore') as log:
                lines = log.readlines()
        except Exception as e:
            print(f"Error reading {f}: {e}")
            continue
            
        for idx, line in enumerate(lines):
            if target in line:
                print(f"Found target in {f} at line {idx+1}:")
                start = max(0, idx - 30)
                end = min(len(lines), idx + 50)
                for j in range(start, end):
                    print(f"{j+1}: {lines[j].strip()}")
                break

if __name__ == '__main__':
    search_timestamp()
