with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if '10013' in line and '16:04' in line:
            print(line.strip())
