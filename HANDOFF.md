# HANDOFF — Crypto_Quant_Bot

**Session: 2026-09-20 | Engine: STOPPED | Git HEAD: 0d6f3c1 (21 ahead of origin/main)**

---

## TL;DR — What happened this session

**3 commits, all reviewed & approved via explicit "approved" gate:**

| # | Work | Commit / Action | Status |
|---|------|-----------------|--------|
| AGENTS.md | Non-Negotiable Rules 1-13 formalized (incl. Rule 8: DB snapshot before write, Rule 13: engine startup = write) | `f694edf` | ✅ Done |
| Finding 2 (exchange layer) | `engine/exchange_interface.py` testnet `fetch_order` now returns `side` + `positionSide` from Binance raw response | `aac94fa` | ✅ Done |
| Task 2 | `tests/conftest.py` DB isolation guard + autouse temp DB + `temp_db` fixture | `0d6f3c1` | ✅ Done |

**DB isolation guard VERIFIED:** write blocked, read-only passes, temp DB works
**Test suite:** 698 collected in `tests/`, 0 collection errors in test dir
**Engine:** STOPPED — explicit operator go-ahead required
**Testnet bots 10008/10018:** PAUSED, `is_active=0`, `status=STOPPED`, orphan exchange positions remain (SOL 0.23, SUI 58.6)

---

## Current Safety State

| Item | Status |
|------|--------|
| Engine | **STOPPED** — explicit operator go-ahead required |
| Bots 10008 (SOL), 10018 (SUI) | **PAUSED / UNRESOLVED** — `is_active=0`, orphan exchange positions (0.23 SOL, 58.6 SUI) |
| is_active guard coverage | **8 sites** (swept, 65-site audit clean) |
| `resolve_net_mismatch()` | **Dependency risk documented** — no independent is_active check; safe only while all 8 upstream writes guarded |
| Startup barrier | **CLEARED** — 8/8 pairs perfect parity |
| Tier-2 health | **CLEAN** — 0 ledger_imbalance |
| DB isolation guard | **ACTIVE & VERIFIED** — live write blocked, mode=ro passes, temp DB works |
| Finding 2 exchange layer | **RESOLVED `aac94fa`** — testnet fetch_order returns side/positionSide |

---

## Open Items (next session starts here)

### 1. Item 5 — `audit_bot_wipes()` signature mismatch (P2)
- **Problem**: Call sites pass positional args; function def uses keyword-only params.
- **Files**: `engine/database.py` (def) + callers in `reconciler.py`, `bot_executor.py`
- **Not started** — needs diff + approval

### 2. Item 7 — Retry-queue cross-ID gap (P2)
- **Problem**: `fill_claims` key = `(bot_id, order_id)` but WS uses `exchange_order_id`, reconciler may use `client_order_id`. Defense-in-depth holds (step lock, dual-write, MAX, bot_orders OR lookup), but test coverage missing.
- **Not started** — needs test + potential fix

### 3. Finding 2 — Side inference gap in parity_gates/database.py (P1)
- **Problem**: `parity_gates.py:1135` (orphan adoption) and `database.py:2173` (race guard) call `credit_fill()` without `side=` param. Physical position may have opposite direction to bot's virtual.
- **Note**: Exchange-layer fix (`aac94fa`) resolved the testnet wrapper; these two sites still need explicit `side=` wiring.
- **Not started** — needs diff + test + approval

### 4. Bot 10008/10018 orphan resolution
- **Status**: Paused, `is_active=0`, real exchange positions exist
- **Blocker**: Requires explicit operator decision (attribution + ledger alignment)
- **Data note**: `trades.cycle_id=39` (should be ~20) — correction pending; `ad7e76e` workaround handles it

### 5. Test Infrastructure — Migration to temp_db fixture (P3)
- **`tests/test_inv35_stuck_dust_no_exit.py`** — hardcodes wrong prod path (`c:\Users\Gionie\...`), triggers isolation guard at fixture setup. Needs migration to `temp_db` fixture.
- **Root-level scripts/archive** — `test_pragma_paths.py`, `scripts/audit_checkpoint_boolean_test.py`, etc. hit guard at collection. Move to `tests/` or mark `@pytest.mark.no_db`.

---

## Residual Risks (documented, not resolved)

1. **`resolve_net_mismatch()` reactivation vector** — promotes `Scanning→IN TRADE` on virtual/physical match; no independent `is_active` check. Safe now (8 guarded upstream writes), but any 9th write path to `trades.total_invested` without `is_active` guard breaks this.

2. **Bot 10008 `trades.cycle_id=39`** — wrong data, code workaround in place (`ad7e76e` respects explicit `cycle_id` and bypasses CARRY for historical). Data correction is separate Rule-8 action.

3. **4 isolation test failures** — `test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views`. Pass in isolation, fail in full-suite (Windows file-lock / shared-DB). Not logic bugs.

---

## Key Files Modified This Session

```
AGENTS.md                              # Rules 1-13 formalized (f694edf)
engine/exchange_interface.py           # Finding 2: testnet fetch_order returns side/positionSide (aac94fa)
tests/conftest.py                      # DB isolation guard + autouse temp DB + temp_db fixture (0d6f3c1)
```

---

## Commands for Next Session

```bash
# Resume from clean state
cd D:/Crypto_Quant_Bot
git status                          # 21 commits ahead, clean working tree

# Verify test baseline (tests/ dir only)
python -m pytest tests/test_ledger_integrity.py tests/test_hedge_lifecycle.py tests/test_database.py tests/test_live_guard_inv30_saturation_guard.py tests/test_exchange_integration.py -v
# Expect: all passed (111+ tests)

# Check engine state
python -c "
from engine.database import get_connection
c=get_connection()
print(c.execute('SELECT id,is_active,status FROM bots WHERE id IN (10008,10018)').fetchall())
"
# Expect: both is_active=0, status=STOPPED

# Verify DB isolation guard
python -c "
import os, sqlite3
os.environ['PYTEST_RUNNING'] = '1'
os.environ['TESTING_MODE'] = 'True'
from tests.conftest import _GUARDED_CONNECT
try:
    _GUARDED_CONNECT('crypto_bot.db')
    print('FAIL: write should be blocked')
except RuntimeError as e:
    print('PASS:', e)
# Read-only
conn = _GUARDED_CONNECT('file:D:/Crypto_Quant_Bot/crypto_bot.db?mode=ro', uri=True)
print('PASS: read-only works')
conn.close()
"

# Review open items
# 1. grep -n "audit_bot_wipes" engine/*.py
# 2. grep -n "fill_claims" engine/ledger.py engine/reconciler.py
# 3. grep -n "credit_fill" engine/parity_gates.py engine/database.py | grep -v "side="
# 4. cat tests/test_inv35_stuck_dust_no_exit.py | head -40
```

---

## Non-Negotiable Rules (from AGENTS.md, re-stated for next agent)

1. **NO engine start** without explicit operator go-ahead
2. **NO DB writes** without Rule-8 snapshot + approval
3. **NO "fixed" claim** without raw output (git diff, pytest, query rows)
4. **Show diff → approval → apply → test** for every code change
5. **Stop after 2 tool failures** on same action — report exact error, change approach
6. **Item-by-item** — complete each numbered item, STOP, wait for approval before next
7. **Raw or nothing** — paste tool output verbatim; never retype, tableize, or say "already pasted"
8. **Evidence over summary** — every claim needs raw output shown
9. **Root cause over patch** — find why, not just make symptom go away
10. **Show before you act** — deletions, resets, credential changes get approval first
11. **Say what you don't know** — guess dressed as fact is worse than "unconfirmed"
12. **Scope discipline** — change only what was approved
13. **Engine startup = write** — any command that could run init_db/heal/backup needs approval

---

## Context Recovery

If context is lost, recover with:
- `session_search(query='20260920 conftest isolation guard', session_id='20260920_175230_93cb02')`
- `session_search(query='aac94fa fetch_order side positionSide', session_id='20260920_175230_93cb02')`
- `session_search(query='f694edf AGENTS.md rules 1-13', session_id='20260920_175230_93cb02')`

---

**End of handoff. Engine stopped. Next session: Item 5 (audit_bot_wipes), Item 7 (retry-queue), Finding 2 parity_gates/database.py side= wiring, Bot 10008/10018 orphan resolution, test_inv35 migration to temp_db fixture.**