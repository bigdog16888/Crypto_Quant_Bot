
with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()[-300:] # Last 300 lines
    for line in lines:
        if 'BotExecutor' in line and ('Error' in line or 'Warning' in line):
            print(line.strip())
