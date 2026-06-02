import glob

def search():
    log_files = glob.glob("engine.log*")
    print(f"Searching in logs: {log_files}")
    
    matches = []
    for log_file in log_files:
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    if "10021" in line and ("Reset to Scanning" in line or "RECONCILE" in line or "force-set" in line or "WIPE" in line):
                        matches.append((log_file, line_num, line.strip()))
        except Exception as e:
            print(f"Error reading {log_file}: {e}")
            
    print(f"Found {len(matches)} matches. Showing all:")
    for match in matches:
        print(f"{match[0]}:{match[1]}: {match[2]}")

if __name__ == '__main__':
    search()
