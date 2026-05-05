with open('engine/bot_executor.py', 'r') as f:
    lines = f.readlines()
# Lines 2444-2448 are indices 2443 to 2447
del lines[2443:2448]
with open('engine/bot_executor.py', 'w') as f:
    f.writelines(lines)
