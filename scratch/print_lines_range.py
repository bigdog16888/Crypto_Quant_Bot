with open('engine/reconciler.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

start = 5340
end = 5375
for j in range(start, end):
    print(f"{j+1}: {lines[j].rstrip()}")
