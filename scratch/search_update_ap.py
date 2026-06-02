import os

def search():
    matches = []
    for root, dirs, files in os.walk("."):
        if ".git" in root or "venv" in root or "env" in root:
            continue
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            if "def update_active_positions_snapshot" in line:
                                matches.append((path, line_num, line.strip()))
                except Exception as e:
                    pass
    print(f"Found {len(matches)} matches:")
    for match in matches:
        print(f"{match[0]}:{match[1]}: {match[2]}")

if __name__ == '__main__':
    search()
