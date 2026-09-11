"""Map connection variable usage (read vs write) in maintain_orders + _reset_to_hedge_standby.
Also check WriteQueue import and save_bot_order internals. Read-only."""
import ast, re

src = open("engine/bot_executor.py", encoding="utf-8").read()
lines = src.split("\n")
tree = ast.parse(src)

WRITE_KW = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)

def sql_of(call):
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return "<dyn>"

def analyze(fn_name):
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            fn = node
            break
    if not fn:
        print(f"{fn_name}: NOT FOUND")
        return
    print(f"\n===== {fn_name} (lines {fn.lineno}-{fn.end_lineno}) =====")
    usage = {}  # recv -> {'read': [lines], 'write': [lines], 'commit': [lines]}
    opens = []  # (line, code) where get_connection / cursor created
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                recv = ast.unparse(f.value)
                if f.attr == "execute":
                    sql = sql_of(node)
                    kind = "write" if (WRITE_KW.search(sql) and not re.match(r"^\s*SELECT", sql, re.I)) else "read"
                    usage.setdefault(recv, {"read": [], "write": [], "commit": []})[kind].append(node.lineno)
                elif f.attr == "commit":
                    usage.setdefault(recv, {"read": [], "write": [], "commit": []})["commit"].append(node.lineno)
                elif f.attr in ("get_connection", "cursor"):
                    opens.append((node.lineno, lines[node.lineno - 1].strip()[:100]))
    for recv, u in sorted(usage.items()):
        if recv in ("exchange", "self.runner", "logger"):
            continue
        print(f"  {recv}: READ {len(u['read'])}x {u['read'][:8]}{'...' if len(u['read'])>8 else ''}")
        print(f"         WRITE {len(u['write'])}x {u['write']}")
        print(f"         COMMIT {len(u['commit'])}x {u['commit']}")
    print("  connection opens:")
    for ln, code in opens:
        print(f"    L{ln}: {code}")

analyze("maintain_orders")
analyze("_reset_to_hedge_standby")

# WriteQueue import present?
print("\n=== WriteQueue import in bot_executor.py ===")
for i, l in enumerate(lines, 1):
    if "WriteQueue" in l or "write_queue" in l:
        print(f"  L{i}: {l.strip()[:110]}")

# save_bot_order: does it route via WriteQueue?
db = open("engine/database.py", encoding="utf-8").read()
m = re.search(r"def save_bot_order\(.*?\n(?=def |\nclass )", db, re.S)
if m:
    body = m.group(0)
    print("\n=== save_bot_order (engine/database.py) ===")
    print("  uses WriteQueue:", "WriteQueue" in body or "put_and_wait" in body)
    print("  first 12 lines:")
    for l in body.split("\n")[:12]:
        print("   ", l[:110])
