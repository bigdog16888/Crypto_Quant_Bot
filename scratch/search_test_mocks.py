import os

def search_test_mocks():
    for root, dirs, files in os.walk('tests'):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if "fetch_closed_orders" in content or "fetchClosedOrders" in content:
                            print(f"Found mock in {path}")
                except Exception as e:
                    pass

if __name__ == '__main__':
    search_test_mocks()
