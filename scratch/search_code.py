import os

def run():
    target = "fetch_closed_orders"
    for root, dirs, files in os.walk('.'):
        if any(p in root for p in ['.git', '.pytest_cache', '__pycache__', 'scratch', 'backups']):
            continue
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        for line_no, line in enumerate(f, 1):
                            if target in line:
                                print(f"{path}:{line_no}: {line.strip()}")
                except Exception:
                    pass

if __name__ == '__main__':
    run()
