import os

def search_today_flatten():
    f = 'engine.log'
    print(f"Searching {f}...")
    try:
        with open(f, 'r', encoding='utf-8', errors='ignore') as log:
            for line in log:
                if line.startswith("2026-05-27"):
                    if "FLATTEN" in line or "flatten" in line or "proof" in line.lower() or "reset" in line.lower():
                        if "sui" in line.lower() or "10018" in line or "100000" in line:
                            print(line.strip())
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    search_today_flatten()
