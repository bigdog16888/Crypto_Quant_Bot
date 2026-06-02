with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

start = 4940
end = 5000
for j in range(start, end):
    print(f"{j+1}: {lines[j].rstrip()}")
