import os

def search():
    for root, dirs, files in os.walk('.'):
        if 'node_modules' in dirs:
            dirs.remove('node_modules')
        if '.git' in dirs:
            dirs.remove('.git')
        for f in files:
            if f.endswith('.py'):
                fp = os.path.join(root, f)
                try:
                    with open(fp, 'r', encoding='utf-8') as file:
                        for i, line in enumerate(file):
                            if 'Tertiary ownership assignment' in line or 'SCAN-FOOTPRINT' in line or 're-linked to residual' in line:
                                print(f"{fp}:{i+1}: {line.strip()}")
                except Exception as e:
                    pass

if __name__ == '__main__':
    search()
