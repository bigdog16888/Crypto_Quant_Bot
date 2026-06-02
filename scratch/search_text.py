import os

def search_text(text):
    for root, dirs, files in os.walk('.'):
        if '.git' in root or '.pytest_cache' in root or '__pycache__' in root:
            continue
        for file in files:
            if file.endswith('.py') or file.endswith('.md'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if text in content:
                            print(f"Found '{text}' in {path}")
                except Exception as e:
                    pass

if __name__ == '__main__':
    search_text("auto_closed")
