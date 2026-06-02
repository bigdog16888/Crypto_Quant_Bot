import os

path = r'config/settings.py'
if not os.path.exists(path):
    print("Error: settings.py not found at", path)
    exit(1)

with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

target = 'VERSION = "3.6.2"'
replacement = 'VERSION = "3.6.4"'

if target in code:
    code = code.replace(target, replacement)
    print("Version bumped in settings.py.")
else:
    print("Target version not found.")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
