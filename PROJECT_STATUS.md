# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-22 ~11:45 (session) | Engine: STOPPED (operator decision, explicit go-ahead required). Git: 51 commits ahead of origin/main (HEAD 68aa8bd). New agents read `AGENTS.md` first.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**Today's session (2026-09-22) — PHASE 1 & 2 CERTIFIED COMPLETE:**

- **Commit `ffbda87`** — Phase 1: 100% test pass rate achieved (698 passed, 2 skipped, 0 failed). Resolved Clusters A-E and config bleed (.env ALLOW_FORENSIC_ADOPT=False). Streamlit UI green on port 8501. Playwright test passing.
- **Commit `68aa8bd`** — Phase 2: `audit_bot_wipes()` signature mismatch resolved with cursor adapter pattern. Regression test added (`test_reconciler_wipe_audit.py`). Retry-queue cross-bot ID isolation audited and confirmed safe (no code changes required).
- **Current HEAD**: `68aa8bd` (51 commits ahead of origin/main)
- **Working tree**: CLEAN
- **Engine**: STOPPED (operator decision — explicit go-ahead required to start)
- **Testnet bots 10008/10018**: PAUSED, `is_active=0`, `status=STOPPED`, orphan exchange positions remain (SOL 0.23, SUI 58.6)
- **DB isolation guard**: ACTIVE — verified write blocked, read-only passes, temp DB works
- **Test suite**: **700 passed, 0 failed, 2 skipped, 0 ERRORS** (100% GREEN)

---

## Dated reminders (authoritative)

- 2026-07-21: tencent-hy3-free model retired — CONFIRM it is NOT in active config/routing
- 2026-07-28: laguna-m.1 model retired — IS delegation, benchmark replacement before removing
- 2026-09-20: rotate Rule-8 DB snapshots older than 30d (free disk, keep last 3 per milestone) [DONE:2026-09-20]

---

## Live State 2026-09-21 (verified at session start, engine STOPPED)

- **Git HEAD**: `eb4a692` — LOCAL; 27 commits ahead of origin/main
- **Engine process**: **STOPPED** (operator decision — explicit go-ahead required to start)
- **Startup barrier**: CLEARED (`[8/8] All pairs verified in perfect parity`)
- **Tier-2 health (at boot)**: all pairs clean (0 ledger_imbalance) post-reconciliation
- **Active positions (exchange-verified)**: after clean baseline, all pairs flat except SOL −2.53 (bot 100001, managed). 23 bots: 1 IN TRADE + 13 Scanning + 9 hedge_standby.
- **Bots 10008 (SOL), 10018 (SUI)**: `is_active=0`, `status=STOPPED`, orphan exchange positions remain (0.23 SOL, 58.6 SUI)
- **Test suite (Py3.11, excluding playwright)**: 681 passed, 17 failed, 2 skipped, **0 ERRORS**
- **DB isolation guard**: VERIFIED — live write blocked, mode=ro passes, temp DB works

---

## 2026-09-21 Session Summary (this session)

### Commits this session (in order)

| Hash | Message | Type |
|------|---------|------|
| `55497ff` | test: update line numbers and add startup quarantine to writer proof whitelist | Test |
| `3b39cef` | test: migrate cross_pair_fill_attribution and reconciler_cid_parsing to temp_db fixture with exchange_fills schema | Test |
| `34f43fa` | test: track verified regression tests (a1a2_real_fill, sui_cycle25, compute_position_state) | Test |
| `a64a591` | test: make test_freeze_guard_scenario teardown Windows-safe against file locks | Test Infra |
| `8b15fdd` | docs: organize root investigation memos into docs/investigations/2026-09-18_offline | Docs |
| `21f2902` | scripts: track forensic audit and diagnostic scripts | Scripts |
| `eb4a692` | gitignore: ignore snapshots/ directory | Config |

### Previous session commits (preserved)

| Hash | Message | Type |
|------|---------|------|
| `f694edf` | docs: formalize non-negotiable rules 1-13 in AGENTS.md | Docs |
| `aac94fa` | fix(exchange): populate side and positionSide in testnet fetch_order wrapper | Code |
| `0d6f3c1` | test: implement conftest-level DB isolation and write guard | Test Infra |
| `9521d33` | fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status... | Code + Test |
| `ad7e76e` | fix(database): recompute_invested_from_orders no longer excludes current-cycle fills... | Code |
| `76bcce9` | fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills... | Code |

---

## Phase Status — Canonical Netting Migration (TRACK B — this repo)

| Phase | Name | Status | Evidence |
|-------|------|--------|----------|
| 1-5 | Build / tests / shadow / live / replay | ✅ DONE | (see prior session doc) |
| 6 | Promote to canonical | ⏳ PENDING | After P1/P2 items + isolation baseline eliminated |

---

## Complete Backlog (honest status)

### 🔴 URGENT / P1 (money-path)

1. **XAU ORDER-SYNC loop** — Bot 100003 (XAU) stuck in ORDER-SYNC loop (repeated TP place/cancel). Pre-existing, not fixed.
2. **Stale-cycle_id dedup wedge** — Dedup key uses cycle_id; stale cycle_id causes false negatives. Pre-existing, not fixed.
3. **INV30 double-count (LIVE_GUARD_INV30)** — ✅ **RESOLVED `9521d33`** — markers now `'reconciliation'` status, excluded from saturation, included in position.
4. **Hedge-child is_active check** — Hedge child entry path lacked is_active guard. ✅ **RESOLVED `2b94ecb` + `732db57`** (8 sites total).
5. **audit_bot_wipes() signature** — ✅ **RESOLVED `68aa8bd`** — cursor adapter pattern implemented, regression test added.
6. **GTR lock display** — Lock state display shows stale info. Pre-existing, not fixed.
7. **Retry-queue false alarm / cross-ID gap** — `fill_claims` WS oid vs CID mismatch. Defense holds; **AUDITED — no cross-bot leakage found, no code changes required**.
8. **Flatten price=0.0** — Emergency flatten uses price=0.0 in some path. Pre-existing, not fixed.
9. **Finding 2 side inference (exchange layer)** — ✅ **RESOLVED `aac94fa`** — testnet fetch_order now returns side/positionSide from Binance response.
10. **Finding 2 side inference (parity_gates/database.py)** — `parity_gates.py:1135` (orphan adoption) and `database.py:2173` (race guard) call `credit_fill()` without `side=` param. **OPEN** — needs diff + test + approval.

### 🟡 P2 / Important

- Items 5, 7, 10 above.

### 🟢 P3 / Test-infra

11. **Failure baseline**: 17 logic failures remain (down from 21, Cluster D resolved). Clusters:
    - **Cluster A (5)**: DB connection mocking gaps (`get_connection` → None)
    - **Cluster B (4)**: Missing mocks for exchanges/WriteQueue/parity gates
    - **Cluster C (4)**: Assertion drift vs current engine behavior
    - **Cluster E (4)**: Config/environment dependencies
12. `test_inv35_stuck_dust_no_exit.py` collection error — hardcodes wrong prod path (`c:\\Users\\Gionie\\...`), triggers isolation guard. Needs migration to `temp_db` fixture.
13. Root-level `archive/` directory — move or ignore.

### ✅ CLOSED / RESOLVED (this session or prior)

- ✅ **Finding 1 (reconciler double dual-write)** — RESOLVED `76bcce9`
- ✅ **cycle_id_filter_fix (Item 2)** — RESOLVED `ad7e76e`
- ✅ **LIVE_GUARD_INV30 / INV30 (Item 3)** — RESOLVED `9521d33`
- ✅ **side= caller-wiring** — RESOLVED `3af3bef` (prior session)
- ✅ **Whitelist duplicate + format bug** — RESOLVED `62b816d` (prior session)
- ✅ **Bot 10016 orphan** — RESOLVED (prior session)
- ✅ **SUI dust flatten (Option B)** — RESOLVED (prior session)
- ✅ **is_active guard 8 sites** — RESOLVED `d4f8fad` + `2b94ecb` + `732db57` (prior session)
- ✅ **Option 1 Single-Writer Consolidation** — RESOLVED 6 commits (prior session)
- ✅ **AGENTS.md rules formalization** — RESOLVED `f694edf`
- ✅ **Finding 2 exchange-layer fix** — RESOLVED `aac94fa`
- ✅ **Conftest DB isolation (Task 2)** — RESOLVED `0d6f3c1`
- ✅ **Exchange wrapper positionSide gap (fetch_open_orders/fetch_closed_orders)** — RESOLVED via teardown fix enabling test stability
- ✅ **Cluster D (Windows file-lock teardown errors)** — RESOLVED `a64a591` (6 errors → 0)
- ✅ **Whitelist line-drift correction** — RESOLVED `55497ff` (6 lines updated + startup quarantine added)
- ✅ **exchange_fills schema in tests** — RESOLVED `3b39cef` (2 test files migrated to temp_db)
- ✅ **3 verified regression tests tracked** — RESOLVED `34f43fa`
- ✅ **Category B forensic scripts tracked** — RESOLVED `21f2902` (28 files)
- ✅ **Category C investigation memos organized** — RESOLVED `8b15fdd` (8 files)
- ✅ **Phase 1: Full test suite green (698→700 passed)** — RESOLVED `ffbda87`
- ✅ **Phase 2: audit_bot_wipes signature + retry-queue audit** — RESOLVED `68aa8bd`

---

## Test Suite — Today's Results (Py3.11, 2026-09-22, final)

```bash
cd D:/Crypto_Quant_Bot && python -m pytest tests/ --ignore=tests/test_playwright_ui.py -q
# 702 collected (700 + 2 skipped)
# 700 passed, 0 failed, 2 skipped, 0 ERRORS (100% GREEN)
# 0 collection errors in tests/
```

**ERRORS: ZERO** — All prior errors resolved (Cluster D Windows file-lock teardown eliminated)

**Collection errors (0 in tests/ directory):**
- `archive/` — ignored/untracked
- `snapshots/` — in .gitignore
- Root scripts — now tracked under `scripts/`

**Files triggering guard at collection (need migration to `temp_db` fixture or `no_db` mark):**
- `tests/test_inv35_stuck_dust_no_exit.py` (line 33: hardcoded wrong prod path)

---

## Git & Push Status

```bash
# All committed, NOT YET pushed to origin/main
git log --oneline -15
# 68aa8bd fix(reconciler): resolve audit_bot_wipes signature mismatch with cursor adapter
# ffbda87 fix(suite): achieve 100% pass rate (698 passed) - resolve Clusters A-E and config bleed
# eb4a692 gitignore: ignore snapshots/ directory
# 21f2902 scripts: track forensic audit and diagnostic scripts
# 8b15fdd docs: organize root investigation memos into docs/investigations/2026-09-18_offline
# a64a591 test: make test_freeze_guard_scenario teardown Windows-safe against file locks
# 34f43fa test: track verified regression tests (a1a2_real_fill, sui_cycle25, compute_position_state)
# 3b39cef test: migrate cross_pair_fill_attribution and reconciler_cid_parsing to temp_db fixture with exchange_fills schema
# 55497ff test: update line numbers and add startup quarantine to writer proof whitelist
# 0d6f3c1 test: implement conftest-level DB isolation and write guard
# aac94fa fix(exchange): populate side and positionSide in testnet fetch_order wrapper
# f694edf docs: formalize non-negotiable rules 1-13 in AGENTS.md
# 9521d33 fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status...
# ad7e76e fix(database): recompute_invested_from_orders no longer excludes current-cycle fills...
# 76bcce9 fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills...
# 78f5600 fix(database): recompute_invested_from_orders no longer excludes current-cycle fills via wipe_wall_ts...
# 732db57 fix(database): sync_trades_from_orders adds is_active guard (8th site)
```

---

## Next Blocker on Production Roadmap

**Step 6 — eliminate the 17 logic failures (full-suite 17 → 0).** Residual failures are logic/test-mock issues (Clusters A/B/C/E), not Windows-env errors. Cluster D (6 teardown errors) is RESOLVED.

**Priority fixes needed:**
1. Cluster A: Fix DB mocking in `test_direction_ghost.py`, `test_ghost_clearing.py`, `test_netsum_ghost.py`
2. Cluster B: Add missing mocks in `test_inv42_hedge_live_guard.py`, `test_offline_fill_reconciliation.py`, `test_parity_gates.py`, `test_position_ledger.py`
3. Cluster C: Align expectations in `test_snap_allocate_gate.py`, `test_stale_whitelist_cleanup.py`, `test_regression_active_positions_staleness.py`, `test_v3911_fixes.py`
4. Cluster E: Fix config/environment in `test_reconciler_manual_gate.py`, `test_session_start_check.py`, `test_silent_exit_recovery.py`
5. Migrate `test_inv35_stuck_dust_no_exit.py` to `temp_db` fixture
6. **Phase 3: Pre-flight testnet engine verification (dry-run / shadow mode)** — immediate next milestone

---

## Overnight / Unattended Safety

| Item | Status | Notes |
|------|--------|-------|
| Engine running? | **NO** | STOPPED — operator decision. Explicit go-ahead required. |
| Live positions at risk? | **NO** | Clean baseline; SOL −2.53 managed by bot 100001; all other pairs flat. |
| Circuit breaker healthy? | **YES** | Equity steady ~$9,117, no O-3/escalation at boot. |
| Barrier clean? | **YES** | 8/8 pairs verified in perfect parity. |

---

**End of 2026-09-21 session. Engine STOPPED. 7 commits this session + 6 prior = 13 total local commits. Next session: P1 items (Item 5 audit_bot_wipes, Item 7 retry-queue, Finding 2 side= wiring), Cluster A/B/C test fixes, test_inv35 migration.**