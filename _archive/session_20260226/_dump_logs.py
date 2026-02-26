with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f, open('10013_log.txt', 'w', encoding='utf-8') as out:
    for line in f:
        if ('10013' in line or '10010' in line) and ('16:03:5' in line or '16:04:' in line):
            out.write(line)
