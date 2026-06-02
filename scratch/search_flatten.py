import os

def search_flatten():
    log_files = [f for f in os.listdir('.') if f.startswith('engine.log')]
    log_files.sort(key=lambda x: int(x.split('.')[-1]) if x.split('.')[-1].isdigit() else 0)
    
    for f in log_files:
        try:
            with open(f, 'r', encoding='utf-8', errors='ignore') as log:
                for line in log:
                    if "FLATTEN" in line or "flatten" in line:
                        print(f"{f}: {line.strip()}")
        except Exception as e:
            pass

if __name__ == '__main__':
    search_flatten()
