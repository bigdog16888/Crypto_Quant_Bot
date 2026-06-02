import os

def search_closed_orders():
    for root, dirs, files in os.walk('tests'):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if "closed_orders" in content.lower():
                            print(f"Found in {path}")
                except Exception as e:
                    pass

if __name__ == '__main__':
    search_closed_orders()
