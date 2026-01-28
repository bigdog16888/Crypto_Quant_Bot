"""
Check if bot is being scanned by the engine
"""
import time

print("Monitoring engine.log for Bot #43 activity...")
print("Press Ctrl+C to stop")
print()

last_size = 0
with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
    f.seek(0, 2)  # Go to end
    last_size = f.tell()

try:
    while True:
        time.sleep(2)
        with open('engine.log', 'r', encoding='utf-8', errors='ignore') as f:
            f.seek(last_size)
            new_lines = f.readlines()
            last_size = f.tell()
            
            for line in new_lines:
                if 'long btc price' in line.lower() or 'bot 43' in line.lower():
                    print(line.strip())
except KeyboardInterrupt:
    print("\nStopped monitoring")
