import glob

def search():
    log_files = glob.glob("engine.log*")
    print(f"Searching in logs: {log_files}")
    
    matches = []
    for log_file in log_files:
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    # Search for log statements in HEDGE-BE-TP registration
                    if "HEDGE-BE-TP" in line or "Failed to register BE TP" in line or "Failed to register BE TP for hedge child" in line or "Failed to write trades for bot" in line:
                        matches.append((log_file, line_num, line.strip()))
        except Exception as e:
            print(f"Error reading {log_file}: {e}")
            
    print(f"Found {len(matches)} matches. Showing all:")
    for match in matches:
        print(f"{match[0]}:{match[1]}: {match[2]}")

if __name__ == '__main__':
    search()
