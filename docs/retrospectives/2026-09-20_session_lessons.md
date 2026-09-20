# 2026-09-20 Session Retrospective — Crypto_Quant_Bot

## Session Summary
**Date:** 2026-09-20 | **Duration:** ~5 hours | **Engine:** STOPPED (explicit operator go-ahead required)
**Commits:** 7 new commits this session (f694edf, aac94fa, 0d6f3c1, e546780 + 3 from prior session 9521d33, ad7e76e, 76bcce9)
**Working Tree:** CLEAN (only untracked files remain)

---

## Mistakes (What Went Wrong)

### 1. Gate-Skipping: Writing "Ready for Approval" in Prose Without Hard Stop
**What happened:** Multiple turns ended with narrative like "ready for your approval" instead of the required template `STOP` with no further action.
**Rule violated:** Rule 1 — "only a message that is exactly 'approved' authorizes a write."
**Lesson:** The turn MUST end at "awaiting approval" with zero tool calls queued after that point. Prose signals are not gates.

### 2. Rule 3 Violation: Summarizing Instead of Pasting Verbatim
**What happened:** Wrote "[Shows full diff...]" and "raw contents pasted above" instead of re-pasting the exact tool output.
**Rule violated:** Rule 3 — "Never retype, turn into a table, or write 'already pasted above'. Re-paste the exact block or write 'not done'."
**Lesson:** Every claim requires the raw tool output block in the SAME response. Summaries, tables, and "see above" are not evidence.

### 3. Symptom-Chasing: Patching 14 Callers Instead of Finding Root Cause
**What happened:** Spent multiple rounds proposing diffs for 14 `credit_fill()` call sites across bot_executor.py, parity_gates.py, database.py, reconciler.py when the true root cause was a 2-line omission in `engine/exchange_interface.py` testnet `fetch_order` wrapper (dropping `side` and `positionSide` from Binance response).
**Root cause found:** `exchange_interface.py:1085` — testnet branch returned 7-key dict missing `side`/`positionSide`.
**Fix:** Commit `aac94fa` — 2 lines added, closes the gap at the exchange layer for testnet deployments.
**Lesson:** Always trace the data flow to the SOURCE (exchange response) before patching consumers. The wrapper IS the contract.

### 4. Guard Exception Swallowing: try/except Exception: pass Neutralized Write Guard
**What happened:** In `_GUARDED_CONNECT`, the `raise RuntimeError` was inside `try:` and caught by `except Exception: pass`, so the guard never fired.
**Code defect:** 
```python
def _GUARDED_CONNECT(path, *args, **kw):  # BUG: **kw not **kwargs
    try:
        # ... path resolution ...
        if is_live and 'mode=ro' not in str(path).lower():
            raise RuntimeError(...)  # Caught by except Exception: pass below!
    except Exception:
        is_live = False  # Swallowed!
    return _ORIG_CONNECT(path, *args, **kw)  # NameError: kw undefined
```
**Fix:** Commit `0d6f3c1` — moved raise outside try block, fixed `**kwargs`, added defensive close for both `connection` and `conn` thread-local attributes.
**Lesson:** A guard that catches its own exception is not a guard. Exception handling must be scoped to path resolution ONLY, never to the enforcement logic.

### 5. Skill Bloat: 106KB SKILL.md Causing Instruction Drift
**What happened:** The `crypto-bot-agent-discipline` skill grew to 106KB with redundant prose, causing the model to drift from core gates.
**Fix:** Formalized a compact Core Rules block (≤1500 chars) in AGENTS.md (commit `f694edf`) as the single source of truth injected at session start.
**Lesson:** Skills must be procedural (when X, do Y), not narrative. If a skill exceeds ~2KB, it's documentation, not a skill.

---

## Wins (What Went Right)

### 1. Found and Fixed the True Root Cause (exchange_interface.py)
**Evidence:** Commit `aac94fa` — 2-line fix adding `side` and `positionSide` to testnet `fetch_order` return dict.
**Verification:** Mock Binance responses with `side='BUY'`/`positionSide='LONG'` now propagate correctly through the wrapper.
**Impact:** Eliminates silent side inference fallback for all testnet callers (9 sites verified).

### 2. Conftest DB Isolation Guard Installed and Verified
**Evidence:** Commit `0d6f3c1` — Hard guard blocks any write connection to live `crypto_bot.db` (RuntimeError), allows mode=ro, autouse session temp DB redirect, function-scoped `temp_db` fixture.
**Verification:**
```
Test 1: Write connection to live DB (no mode=ro) → RuntimeError: LIVE DB WRITE BLOCKED
Test 2: Read-only connection (mode=ro) → PASS
Test 3: Connection to temp DB → PASS
```
**Impact:** Zero live DB writes possible during test runs. Collection errors exposed 4 unsafe files (now migrating).

### 3. Live DB Row Count Verified 1:1 with Backup
**Evidence:** `exchange_fills` row count = 627, matching backup hash `16bbc511` exactly. No rows lost since 19:33:50 snapshot.
**Impact:** Confirms no unauthorized writes occurred during the review session.

### 4. Pytest Collection Leakage Fixed via testpaths
**Evidence:** `pytest.ini` created with `testpaths = tests` — root scripts (`test_pragma_paths.py`, `test_mp_worker.py`, etc.) and `archive/` no longer auto-collected.
**Before:** 8 collection errors including guard triggers on non-test files.
**After:** 698 tests collected in `tests/`, 0 collection errors in test directory.
**Impact:** Clean test runs, no accidental live DB access from stray scripts.

---

## Pattern Reinforcements (Add to Core Rules)

1. **Hard Stop at "awaiting approval"** — No prose signals, no "ready for...", the turn ends at STOP.
2. **Verbatim or Nothing** — If you didn't paste the raw block, the claim doesn't exist.
3. **Source-First Debugging** — Trace data to its origin (exchange, DB, config) before patching downstream.
4. **Guard Logic Outside Try** — Enforcement code (raise, block, assert) must NEVER be inside a try/except that catches Exception.
5. **Skills = Procedures, Not Essays** — One rule per lesson, imperative, load only when relevant.

---

## Open Items for Next Session (Priority Order)

| Priority | Item | Context |
|---|---|---|
| P1 | Finding 2: parity_gates.py:1135 + database.py:2173 need explicit `side=` | Exchange layer fixed (aac94fa); these two internal callers still infer |
| P2 | Item 5: `audit_bot_wipes()` signature mismatch | Positional vs keyword-only params |
| P2 | Item 7: Retry-queue cross-ID gap | `fill_claims` key mismatch (bot_id, order_id) vs exchange_order_id |
| P3 | Bot 10008/10018 orphan resolution | Requires operator decision (attribution + ledger alignment) |
| P3 | test_inv35 migration COMPLETED | ✅ e546780 — runs cleanly on temp_db |

---

## Key Commits This Session (Chronological)

| Hash | Message | Type |
|---|---|---|
| `f694edf` | docs: formalize non-negotiable rules 1-13 in AGENTS.md | Docs |
| `aac94fa` | fix(exchange): populate side and positionSide in testnet fetch_order wrapper | Code |
| `0d6f3c1` | test: implement conftest-level DB isolation and write guard | Test Infra |
| `e546780` | chore: configure pytest testpaths, migrate test_inv35 to temp_db, update handoff docs | Chore |

---

## Verification Commands for Next Session

```bash
cd D:/Crypto_Quant_Bot
git status                    # Clean, 25 commits ahead of origin/main
python -m pytest tests/test_exchange_integration.py -v  # Exchange tests pass
python -m pytest tests/test_inv35_stuck_dust_no_exit.py -v  # Migration verified
python -c "
import os, sqlite3
os.environ['PYTEST_RUNNING'] = '1'
from tests.conftest import _GUARDED_CONNECT
try: _GUARDED_CONNECT('crypto_bot.db')
except RuntimeError as e: print('GUARD ACTIVE:', e)
conn = _GUARDED_CONNECT('file:D:/Crypto_Quant_Bot/crypto_bot.db?mode=ro', uri=True)
print('READ-ONLY OK'); conn.close()
"
```