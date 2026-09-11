"""Verification gate: syntax, internal-fn cross-check, import smoke."""
import ast, re, sys, os

PATH = "engine/bot_executor.py"
src = open(PATH, encoding="utf-8").read()

# 1. Syntax
tree = ast.parse(src)
print("1. SYNTAX OK")

# 2. Internal fn cross-check
mod_internals = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.endswith("_internal")}
called = set(re.findall(r"put_and_wait\(\s*\n?\s*(_\w+_internal)", src))
missing = called - mod_internals
unused = mod_internals - called
print(f"2. internals defined={len(mod_internals)} called={len(called)} missing={missing or 'NONE'} unused={unused or 'NONE'}")
if missing:
    sys.exit(1)

# 3. Signature check: every put_and_wait call arg count matches def param count
defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.endswith("_internal")}
bad = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "put_and_wait":
        if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in defs:
            d = defs[node.args[0].id]
            nparams = len(d.args.args)
            nargs = len(node.args) - 1
            if nargs != nparams:
                bad.append((node.args[0].id, nparams, nargs, node.lineno))
print(f"3. signature mismatches: {bad or 'NONE'}")
if bad:
    sys.exit(1)

# 4. Import smoke with test-DB isolation
os.environ["CQB_DB_PATH"] = os.path.join(os.getcwd(), "_smoke_test.db")
sys.path.insert(0, os.getcwd())
try:
    import engine.bot_executor as be
    print(f"4. IMPORT OK: {be.__file__}")
    # Verify key symbols exist
    for sym in ["_reset_to_hedge_standby_internal", "_reset_to_hedge_standby_status_force_internal",
                "_maintain_tp_clear_internal", "_maintain_hedge_freeze_internal", "BotExecutor"]:
        assert hasattr(be, sym), f"missing symbol {sym}"
    print("   all key symbols present")
except Exception as e:
    print(f"4. IMPORT FAILED: {type(e).__name__}: {e}")
    sys.exit(1)
finally:
    if os.path.exists("_smoke_test.db"):
        os.remove("_smoke_test.db")

print("\nALL GATES PASSED")
