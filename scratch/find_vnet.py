import os

def find_vnet():
    for root, dirs, files in os.walk('engine'):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    if 'get_pair_virtual_net' in content:
                        print(f"Found in {path}")

if __name__ == "__main__":
    find_vnet()
