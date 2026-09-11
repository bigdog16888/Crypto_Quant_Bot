"""Verify: parse OK, WriteQueue imported, all put_and_wait-referenced _internal fns defined."""
import ast, re

src = open("engine/bot_executor.py", encoding="utf-8").read()

# 1. parse
try:
    tree = ast.parse(src)
    print("PARSE: OK")
except SyntaxError as e:
    print(f"PARSE: FAIL {e}")
    raise SystemExit(1)

# 2. WriteQueue import
imp = [l for l in src.splitlines() if "WriteQueue" in l and ("import" in l)]
print("WriteQueue import lines:", imp)

# 3. defined _internal fns
defined = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_internal"):
        defined.add(node.name)
print(f"Defined _internal fns: {len(defined)}")

# 4. referenced in put_and_wait calls
refs = set(re.findall(r"put_and_wait\(\s*(\w+)", src))
missing = sorted(r for r in refs if r not in defined)
print(f"Referenced via put_and_wait: {len(refs)}")
print(f"MISSING (referenced but not defined): {missing if missing else 'NONE'}")

# 5. B1 specific
print("_reset_to_hedge_standby_internal defined:", "_reset_to_hedge_standby_internal" in defined)
print("_reset_to_hedge_standby_internal referenced:", "_reset_to_hedge_standby_internal" in refs)
