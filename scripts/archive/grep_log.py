import re, datetime, sys

dt_regex = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
now = datetime.datetime.now()
past = now - datetime.timedelta(minutes=20)

res = []
try:
    with open('engine.log', encoding='utf-8', errors='ignore') as f:
        for l in f:
            m = dt_regex.match(l)
            if m:
                try:
                    dt = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                    if dt >= past and 'bot_executor' in l:
                        res.append(l.strip())
                except:
                    pass
except Exception as e:
    print(f"Error reading log: {e}")

with open('engine_log_filter.txt', 'w', encoding='utf-8') as out:
    out.write('\n'.join(res[:50]))
