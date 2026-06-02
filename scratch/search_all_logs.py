import os

def search_all_logs():
    log_files = [f for f in os.listdir('.') if f.startswith('engine.log')]
    # Sort files: engine.log is newest, then engine.log.1, engine.log.2, etc.
    # We want to search in chronological order, so newest to oldest
    log_files.sort(key=lambda x: int(x.split('.')[-1]) if x.split('.')[-1].isdigit() else 0)
    
    print(f"Log files found: {log_files}")
    for f in log_files:
        print(f"\n--- Searching {f} ---")
        try:
            with open(f, 'r', encoding='utf-8', errors='ignore') as log:
                for line in log:
                    if "97816674" in line or "104538" in line:
                        print(f"{f}: {line.strip()}")
        except Exception as e:
            print(f"Error reading {f}: {e}")

if __name__ == '__main__':
    search_all_logs()
