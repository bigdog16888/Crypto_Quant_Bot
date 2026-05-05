import re
with open("engine/bot_executor.py", "r", encoding="utf-8") as f:
    text = f.read()

matches = re.finditer(r"def .*hedge", text, re.IGNORECASE)
for m in matches:
    print(m.group(0))
