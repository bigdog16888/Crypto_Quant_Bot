import os

def search_reset_proof():
    for root, dirs, files in os.walk('.'):
        if '.git' in root or '.pytest_cache' in root or '__pycache__' in root:
            continue
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if "RESET_WITH_PROOF" in content or "Found exit fill" in content:
                            print(f"Found in {path}")
                except Exception as e:
                    pass

if __name__ == '__main__':
    search_reset_proof()
