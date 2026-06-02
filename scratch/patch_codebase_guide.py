import os

path = r'CODEBASE_GUIDE.md'
if not os.path.exists(path):
    print("Error: CODEBASE_GUIDE.md not found at", path)
    exit(1)

with open(path, 'r', encoding='utf-8', errors='replace') as f:
    code = f.read()

target = """This is the permanent fix for MARGIN HELD on SHORT bots in net-LONG pairs (and vice versa).

---"""

replacement = """This is the permanent fix for MARGIN HELD on SHORT bots in net-LONG pairs (and vice versa).

### 3.22. Hedge Child base_size Config Bypass (v3.6.4)

INVARIANT: Hedge child bots have `base_size = 0` by design. They never place independent entries.

The strict `base_size < exchange_min_notional` config check in `process_bot` (bot_executor.py) MUST be bypassed entirely for `bot_type = 'hedge_child'`. Bypassing this guard ensures that hedge child bots are not halted with `Config Error` at startup.

---"""

if target in code:
    code = code.replace(target, replacement)
    print("CODEBASE_GUIDE.md updated successfully.")
else:
    # Try with different line endings
    target_crlf = target.replace("\n", "\r\n")
    replacement_crlf = replacement.replace("\n", "\r\n")
    if target_crlf in code:
        code = code.replace(target_crlf, replacement_crlf)
        print("CODEBASE_GUIDE.md updated successfully (CRLF).")
    else:
        print("Target section not found in CODEBASE_GUIDE.md.")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
