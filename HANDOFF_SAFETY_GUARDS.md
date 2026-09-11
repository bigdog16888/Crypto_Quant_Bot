# Handoff — Crypto_Quant_Bot Safety-Guard Work (2026-08-20)

Branch: `refactor/inv31-bot_executor-writequeue`. Py 3.10.11 (CCXT-pinned engine env: `C:\Users\Gionie\AppData\Local\Programs\Python\Python310\python.exe`).

## DONE + VERIFIED (real test output & diffs shown this session)

| Item | What | Files | Test result |
|------|------|-------|-------------|
| **O-1** | Position Sizing UI page + sizing circuit breaker (earlier session) | `ui/views/position_sizing.py`, `ui/app.py` | Verif'd in earlier session (AppTest `$9,250.23` equity) |
| **O-9** | Startup matched-pair plausibility gate (Step 2.5) | `engine/runner/startup.py`, `engine/ledger.py` | 10/10 (`test_startup_plausibility_gate.py`) |
| — | Foreign-symbol classification (Pattern E companion) | `engine/runner/startup.py` | 6/6 (`test_startup_barrier_foreign_symbols.py`) |
| **O-3** | Rolling-window (24h/20%) portfolio drawdown breaker | `engine/database.py` (helpers+DDL), `engine/runner/__init__.py`, `config/settings.py` | 6/6 (`test_drawdown_breaker.py`) |
| **O-10** | Hedge-engagement watchdog (parent-only freeze; ≥2 in-24h window → whole-engine halt) | `engine/hedge_watchdog.py` (new), `engine/bot_executor.py`, `config/settings.py` | 10/10 (`test_hedge_watchdog.py`) |

**All-new tests this session: 32/32 PASS.** All 5 touched source files compile clean under 3.10.11.
Diffs reviewed: fixed `_base`/`base` NameError in O-3 runner block (caught & fixed this session); bot_executor.py O-10 hunk confirmed scoped (~24 lines, INV-31 changes excluded).

## FULL-SUITE STATE (this session, Py 3.10.11)
`515 passed, 17 failed, 3 warnings, 4 subtests passed` **+ 1 collection-blocking error** = **18 known issues.**

**Collection-blocking error (1):**
- `tests/test_position_size_circuit_breaker.py` — imports `_get_bot_config_params` / `_calculate_config_max_notional` from `engine.database` which **do not exist**. Untracked O-1-related test referencing never-landed helpers. Blocks whole-suite collection.

**Pre-existing failures (17) — NONE in O-1/O-3/O-9/O-10 touched files:**
- `test_adopt_fill_guard.py` (4) — fill-guard reset path
- `test_auto_repair_guards.py` (1)
- `test_ghost_clearing.py` (2) — ghost-clearing hedged reset
- `test_hedge_lifecycle.py` (1) — ticket5 global netting
- `test_inv31_ground_truth_reconciler.py` (1)
- `test_inv35_stuck_dust_no_exit.py` (1)
- `test_inv38_netting_aware_close.py` (1)
- `test_parity_gates_retry.py` (1)
- `test_require_proof_writers.py` (2)
- `test_seal_short_phantom.py` (1)
- `test_snap_allocate_gate.py` (1)
- `test_streamlit_smoke.py` (1) — `test_database_views` AttributeError (DB isolation under full-suite run)

## OPEN / STILL-TO-DO
1. **19 (now 18) pre-existing test failures** — need triage: are they real regressions, stale test expectations, or DB-isolation artifacts of full-suite parallel runs? (`test_streamlit_smoke.py::test_database_views` is clearly a shared-DB-path isolation issue; others need investigation.)
2. **`test_position_size_circuit_breaker.py`** — collection-blocking: either land the missing `_get_bot_config_params`/`_calculate_config_max_notional` helpers into `engine.database.py`, or fix/remove the test.
3. **O-2 (`tests/test_o2_unify_netting.py`)** — test exists, **passes 5/5 standalone**, but its source changes (`engine/oneway_netting.py` +251/-76, `engine/parity_gates.py` +23) are **uncommitted/undiffed** against the branch — needs a diff review + a home in the history, not just a passing local test.
4. **Position Sizing UI** (O-1) — done on `ui/views/position_sizing.py` but untracked; needs commit.

## ENV / WORKFLOW NOTES (for next time)
- **Python 3.10** pinned for engine/CCXT (`PYTHONPATH` leak under Hermes 3.11 → `0xc0000139`). Use `python.exe -m pytest` from Py310.
- CRLF files (`config/settings.py`, `engine/bot_executor.py`, `engine/runner/__init__.py`) fight the fuzzy patch matcher → use byte-exact Python `io.open(..., newline='')` scripts or `execute_code` for edits on those files. `engine/database.py` is LF.
- Full suite is large (~125s) and has a collection-blocking test that must be `--ignore`d to run the rest.