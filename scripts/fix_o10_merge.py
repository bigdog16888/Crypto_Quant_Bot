"""Restore HEAD's coherent O-10 watchdog + call site after bad conflict resolution.

The two lineages evolved O-10 differently:
- HEAD (kanban-approved t_bee48224 + t_26d5981e): engagement watchdog with grace
  window; bot_executor calls verify_hedge_engagement.
- d53937b lineage: older netting rewrite; calls verify_netting_engagement.

HEAD's version is canonical (committee-reviewed and approved). This script
checks out both files wholesale from HEAD, then re-applies the ONLY thing
d53937b's side had that we still need in bot_executor.py: the Phase-1
hedge-live-guard non-ok status skip logic, which lives ~700 lines earlier and
was auto-merged cleanly, and the timeout params in database.py, also clean.
"""
import subprocess

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd='.')
    print(f'$ {cmd}\n{r.stdout}{r.stderr}')
    return r

run('git checkout HEAD -- engine/hedge_watchdog.py')

# Restore the HEAD O-10 call-site block in bot_executor.py (d53937b's call
# leaked in via the auto-merge hunk around the conflict).
with open('engine/bot_executor.py', 'r', encoding='utf-8') as f:
    content = f.read()

d_call = """                        from engine.hedge_watchdog import verify_netting_engagement
                        _wd = verify_netting_engagement(parent_bot_id=bot_id,
                                                       parent_direction=direction,
                                                       conn=_conn_hcs,
                                                       exchange=exchange,
                                                       config=config)"""
h_call = """                        from engine.hedge_watchdog import verify_hedge_engagement
                        _wd = verify_hedge_engagement(parent_bot_id=bot_id,
                                                     parent_direction=direction,
                                                     conn=_conn_hcs,
                                                     config=config)"""

if d_call in content:
    content = content.replace(d_call, h_call)
    print('bot_executor.py: O-10 call site restored to verify_hedge_engagement')
elif h_call in content:
    print('bot_executor.py: O-10 call site already correct')
else:
    print('bot_executor.py: WARNING — neither call pattern found')

with open('engine/bot_executor.py', 'w', encoding='utf-8') as f:
    f.write(content)

run('git add engine/hedge_watchdog.py engine/bot_executor.py')
run('python -m py_compile engine/hedge_watchdog.py engine/bot_executor.py')
