"""Dump compact context (±6 lines) around each maintain_orders WRITE site.
Read-only. Helps craft put_and_wait replacements."""
import ast, re

SRC = "engine/bot_executor.py"
lines = open(SRC, encoding="utf-8").read().split("\n")
src = "\n".join(lines)
tree = ast.parse(src)

mo = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "maintain_orders":
        mo = node
        break

WRITE_KW = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
SELECT_KW = re.compile(r"^\s*SELECT\b", re.I)

def get_sql(call):
    if call.args:
        a = call.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return "<dynamic>"

# collect write execute calls + commit calls
writes = []
commits = []
for node in ast.walk(mo):
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr == "execute":
                sql = get_sql(node)
                if WRITE_KW.search(sql) and not SELECT_KW.search(sql):
                    recv = ast.unparse(f.value)
                    writes.append((node.lineno, recv, sql))
            elif f.attr == "commit":
                commits.append(node.lineno)

writes.sort()
print(f"# {len(writes)} write sites, {len(commits)} commits\n")
for ln, recv, sql in writes:
    sql1 = " ".join(sql.split())
    print(f"===== L{ln} recv={recv}  SQL: {sql1[:100]}")
    lo = max(0, ln - 7)
    hi = min(len(lines), ln + 6)
    for i in range(lo, hi):
        marker = ">>" if (i + 1) == ln else "  "
        print(f"{marker}{i+1:5d}| {lines[i]}")
    print()
