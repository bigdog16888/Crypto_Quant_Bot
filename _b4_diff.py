import re, os
os.chdir(r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot_INV31")

def norm(s):
    return re.sub(r"\s+", " ", s).strip()

cur_src = open("engine/oneway_netting.py", encoding="utf-8").read()
pre_src = open("_on_pre_refactor.py", encoding="utf-8").read()

# CURRENT module constants
def const(name):
    m = re.search(rf"{name} = \((.*?)\n\)", cur_src, re.S)
    if not m:
        m = re.search(rf'{name} = "(.*?)"', cur_src, re.S)
        return norm(m.group(1)) if m else None
    return norm("".join(re.findall(r'"([^"]*)"', m.group(1))))

cur_status = const("_GHOST_WIPE_BOT_STATUS_SQL")
cur_cancel = const("_GHOST_WIPE_CANCEL_ORDERS_SQL")
cur_reset  = const("_GHOST_WIPE_RESET_ORDERS_SQL")
cur_phase  = const("_GHOST_WIPE_CYCLE_PHASE_SQL")

# PRE inline literals: extract each conn.execute("...") string in wipe_bot_ghost
m = re.search(r"^def wipe_bot_ghost\(.*?(?=^def |\Z)", pre_src, re.S | re.M)
pre_fn = m.group(0)
# each execute: conn.execute(\n  "lit" "lit" ..., args)
pre_stmts = []
for blk in re.finditer(r"conn\.execute\(\s*((?:\"[^\"]*\"\s*)+)", pre_fn):
    pre_stmts.append(norm("".join(re.findall(r'"([^"]*)"', blk.group(1)))))

print("PRE statements (in order):")
for i, s in enumerate(pre_stmts):
    print(f"  [{i}] {s}")

print("\nCURRENT constants:")
print("  status:", cur_status)
print("  cancel:", cur_cancel)
print("  reset :", cur_reset)
print("  phase :", cur_phase)

# Map: pre[0]=SELECT (read), pre[1]=bots status, pre[2]=cancel, pre[3]=reset, pre[4]=bots status re-force, pre[5]=cycle_phase
checks = [
    ("bots.status write",      pre_stmts[1], cur_status),
    ("cancel orders filter",   pre_stmts[2], cur_cancel),
    ("reset_cleared filter",   pre_stmts[3], cur_reset),
    ("bots.status re-force",   pre_stmts[4], cur_status),
    ("cycle_phase IDLE",       pre_stmts[5], cur_phase),
]
print("\n=== STATEMENT-BY-STATEMENT COMPARISON ===")
all_ok = True
for label, pre, cur in checks:
    ok = pre == cur
    all_ok &= ok
    print(f"  {'IDENTICAL' if ok else 'DIFFERS!!'}  {label}")
    if not ok:
        print(f"      pre: {pre}")
        print(f"      cur: {cur}")
print("\nVERDICT:", "ALL FILTERS BYTE-IDENTICAL (whitespace-normalized)" if all_ok else "SEMANTIC CHANGE DETECTED")
