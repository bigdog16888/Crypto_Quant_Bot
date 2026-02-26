import json

with open("engine.log", "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

bots = {}
for line in lines:
    if "Triggers:" in line:
        try:
            bot_name = line.split("[")[1].split("]")[0]
            bots[bot_name] = line.strip()
        except:
            pass

print("=== LATEST TRIGGER EVALUATIONS ===")
for b, l in sorted(bots.items()):
    val = l.split("Triggers:")[1].strip()
    print(f"{b}: {val}")
