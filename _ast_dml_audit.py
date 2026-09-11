"""AST analysis: for each engine module, list trades/bot_orders DML statements
and the name of the enclosing function, so we can see which are inside
WriteQueue *_internal functions vs bare."""
import ast, os, re, sys

DML_RE = re.compile(
    r"\b(INSERT\s+INTO|INSERT\s+OR\s+\w+\s+INTO|UPDATE|DELETE\s+FROM)\s+(trades|bot_orders)\b",
    re.IGNORECASE,
)

MODULES = [
    'bot_executor', 'ground_truth_reconciler', 'oneway_netting', 'parity_gates',
    'ws_event_handlers', 'reconciler_wipe_audit', 'wipe_proof',
    'ledger', 'database', 'reconciler', 'cycle_loop', 'recovery', 'exchange_interface',
]

def first_str_arg(node):
    """Extract SQL string from conn.execute('...', ...) / cursor.execute(...)"""
    if not (isinstance(node, ast.Call) and node.args):
        return None
    a0 = node.args[0]
    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
        return a0.value
    if isinstance(a0, ast.JoinedStr):  # f-string — collect static parts
        parts = []
        for v in a0.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
        return ''.join(parts)
    return None

def analyze(path):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    hits = []
    # walk with enclosing-function tracking
    def visit(node, func_stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_stack = func_stack + [node.name]
        if isinstance(node, ast.Call):
            sql = first_str_arg(node)
            if sql and DML_RE.search(sql):
                m = DML_RE.search(sql)
                hits.append((node.lineno, func_stack[-1] if func_stack else '<module>',
                             m.group(1).upper() + ' ' + m.group(2)))
        for ch in ast.iter_child_nodes(node):
            visit(ch, func_stack)
    visit(tree, [])
    return hits

base = 'engine'
for mod in MODULES:
    candidates = [os.path.join(base, mod + '.py'),
                  os.path.join(base, 'runner', mod + '.py')]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        print(f"## {mod}: FILE NOT FOUND")
        continue
    hits = analyze(path)
    if not hits:
        print(f"## {mod}: no direct trades/bot_orders DML")
        continue
    bare = [h for h in hits if not h[1].endswith('_internal')]
    print(f"## {mod}: {len(hits)} DML sites, {len(bare)} outside *_internal")
    for lineno, func, op in hits:
        marker = 'BARE ' if not func.endswith('_internal') else '     '
        print(f"  {marker}L{lineno:<5} {op:<22} in {func}")
