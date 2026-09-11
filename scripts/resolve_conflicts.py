import re

for path in ['engine/bot_executor.py', 'engine/hedge_watchdog.py']:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    resolved = re.sub(r'<<<<<<< HEAD\n(.*?)=======\n.*?>>>>>>> d53937b\n', r'\1', content, flags=re.DOTALL)
    n = content.count('<<<<<<< HEAD')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(resolved)
    print(f'{path}: resolved {n} conflict(s), markers remaining: {resolved.count("<<<<<<<")}')
