import os, glob
print('=== INVENTORY: snapshot*.db ===')
for p in sorted(glob.glob('snapshot*.db'), key=os.path.getmtime, reverse=True):
    sz = os.path.getsize(p)
    mtime = __import__('datetime').datetime.fromtimestamp(os.path.getmtime(p))
    print(f'{p:<55} {sz:>12,} bytes  {mtime}')
print()
