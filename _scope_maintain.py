"""Scope the maintain_orders cluster: list every conn .execute write site with its SQL + target table.
Read-only analysis of HEAD engine/bot_executor.py. No mutation."""
import re, ast, sys

SRC = "engine/bot_executor.py"
src = open(SRC, encoding="utf-8").read()
lines = src.split("\n")

# Locate maintain_orders method boundaries via AST
tree = ast.parse(src)
mo = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "maintain_orders":
        mo = node
        break
if mo is None:
    print("maintain_orders NOT FOUND"); sys.exit(1)

start = mo.lineno
end = mo.end_lineno
print(f"maintain_orders: lines {start}-{end} ({end-start} lines)")

# Walk AST for .execute(...) calls within maintain_orders that are writes
WRITE_KW = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
SELECT_KW = re.compile(r"^\s*SELECT\b", re.I)

def get_sql(call):
    # first positional arg
    if call.args:
        a = call.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
        if isinstance(a, ast.JoinedStr):
            return "<f-string>"
    return "<dynamic>"

sites = []
for node in ast.walk(mo):
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            sql = get_sql(node)
            is_write = bool(WRITE_KW.search(sql)) and not SELECT_KW.search(sql)
            # table detection
            m = re.search(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+([A-Za-z_]+)", sql, re.I)
            table = m.group(2) if m else "?"
            # receiver name
            recv = ""
            v = func.value
            if isinstance(v, ast.Name):
                recv = v.id
            elif isinstance(v, ast.Attribute):
                recv = ast.unparse(v)
            sites.append((node.lineno, recv, table, is_write, sql.strip().replace("\n", " ")[:90]))

print(f"\nTotal .execute calls in maintain_orders: {len(sites)}")
writes = [s for s in sites if s[3]]
reads = [s for s in sites if not s[3]]
print(f"WRITE sites: {len(writes)}   READ sites: {len(reads)}\n")

print("=== WRITE SITES (line, receiver, table, sql) ===")
from collections import Counter
pat_count = Counter()
for ln, recv, table, _, sql in sorted(writes):
    print(f"L{ln:5d}  {recv:22s}  {table:12s}  {sql}")
    pat_count[(table, sql[:60])] += 1

print("\n=== DISTINCT (table, sql-prefix) patterns ===")
for (table, sqlp), n in pat_count.most_common():
    print(f"  x{n:2d}  {table:12s}  {sqlp}")

# Also find .commit() calls in maintain_orders
commits = []
for node in ast.walk(mo):
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "commit":
            commits.append(node.lineno)
print(f"\n.commit() calls in maintain_orders: {len(commits)} at lines {sorted(commits)}")
