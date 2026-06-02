def search():
    log_file = "engine.log"
    print(f"Reading last 1000 lines of {log_file}...")
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            # print lines that contain RECON or MISMATCH or GLOBAL-NET
            last_lines = lines[-1000:]
            for line_num, line in enumerate(last_lines, len(lines) - 999):
                if any(x in line for x in ["RECON", "MISMATCH", "GLOBAL-NET", "SYSTEM-NET", "WIPE"]):
                    print(f"{line_num}: {line.strip()}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    search()
