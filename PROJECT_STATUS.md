# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-20 ~22:40 (session) | Engine: STOPPED (operator decision, explicit go-ahead required). Git: 21 commits ahead of origin/main (HEAD 0d6f3c1). New agents read `AGENTS.md` first.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**Today's session (2026-09-20) — MAJOR INFRASTRUCTURE FIXES COMMITTED:**

- **Commit `f694edf`** — AGENTS.md formalized with Non-Negotiable Rules 1-13 (including Rule 8: DB snapshot before write, Rule 13: engine startup = write). All commits in this review session required explicit "approved" gate.
- **Commit `aac94fa`** — Finding 2 root cause fixed: `engine/exchange_interface.py` testnet `fetch_order` wrapper now populates `side` and `positionSide` from Binance raw response. Closes the side-inference gap at the exchange layer for testnet deployments. Caller sweep (9 sites) verified.
- **Commit `0d6f3c1`** — Task 2 complete: `tests/conftest.py` DB isolation guard installed. Hard guard blocks any write connection to live `crypto_bot.db` (RuntimeError). Autouse session-scoped temp DB redirect for all tests. Function-scoped `temp_db` fixture for explicit isolation. Defensive `_force_close_cached_conn()` closes both `connection` and `conn` thread-local attributes.
- **Current HEAD**: `0d6f3c1` (21 commits ahead of origin/main)
- **Working tree**: CLEAN (only untracked files remain)
- **Engine**: STOPPED (operator decision — explicit go-ahead required to start)
- **Testnet bots 10008/10018**: PAUSED, `is_active=0`, `status=STOPPED`, orphan exchange positions remain (SOL 0.23, SUI 58.6)
- **DB isolation guard**: ACTIVE — verified write blocked, read-only passes, temp DB works
- **Test suite**: 698 collected, 0 collection errors in `tests/` (ignoring archive/scripts root files), 8 errors in non-test files (expected)

---

## Dated reminders (authoritative)

- 2026-07-21: tencent-hy3-free model retired — CONFIRM it is NOT in active config/routing
- 2026-07-28: laguna-m.1 model retired — IS delegation, benchmark replacement before removing
- 2026-09-20: rotate Rule-8 DB snapshots older than 30d (free disk, keep last 3 per milestone) [DONE:2026-09-20]

---

## Live State 2026-09-20 (verified at session start, engine STOPPED)

- **Git HEAD**: `0d6f3c1` — LOCAL; 21 commits ahead of origin/main
- **Engine process**: **STOPPED** (operator decision — explicit go-ahead required to start)
- **Startup barrier**: CLEARED (`[8/8] All pairs verified in perfect parity`)
- **Tier-2 health (at boot)**: all pairs clean (0 ledger_imbalance) post-reconciliation
- **Active positions (exchange-verified)**: after clean baseline, all pairs flat except SOL −2.53 (bot 100001, managed). 23 bots: 1 IN TRADE + 13 Scanning + 9 hedge_standby.
- **Bots 10008 (SOL), 10018 (SUI)**: `is_active=0`, `status=STOPPED`, orphan exchange positions remain (0.23 SOL, 58.6 SUI)
- **Test suite (Py3.11, excluding playwright)**: 698 tests collected in `tests/`, 0 collection errors in test dir
- **DB isolation guard**: VERIFIED — live write blocked, mode=ro passes, temp DB works

---

## 2026-09-20 Session Summary (this session)

### Commits this session (in order)

| Hash | Message | Type |
|------|---------|------|
| `f694edf` | docs: formalize non-negotiable rules 1-13 in AGENTS.md | Docs |
| `aac94fa` | fix(exchange): populate side and positionSide in testnet fetch_order wrapper | Code |
| `0d6f3c1` | test: implement conftest-level DB isolation and write guard | Test Infra |

### Previous session commits (preserved)

| Hash | Message | Type |
|------|---------|------|
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
5. **audit_bot_wipes() signature** — Caller passes positional args; def expects keyword-only. **OPEN** — not started.
6. **GTR lock display** — Lock state display shows stale info. Pre-existing, not fixed.
7. **Retry-queue false alarm / cross-ID gap** — `fill_claims` WS oid vs CID mismatch. Defense holds; test missing. **OPEN** — not started.
8. **Flatten price=0.0** — Emergency flatten uses price=0.0 in some path. Pre-existing, not fixed.
9. **Finding 2 side inference (exchange layer)** — ✅ **RESOLVED `aac94fa`** — testnet fetch_order now returns side/positionSide from Binance response.

### 🟡 P2 / Important

- Items 5, 7 above.

### 🟢 P3 / Test-infra

9. `-2015` burst during emergency (parked).
10. **Failure baseline**: 4 isolation artifacts (pass in isolation, fail in full-suite): `test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views` — shared-DB / Windows file-lock contamination.
11. **`close_position()` symbol-normalization bug** (2026-09-15): `bots.pair` includes `:USDC` suffix; exchange `fetch_positions` returns bare symbol. Mismatch → wipes ledger without ordering. Fix: normalize before matching.
12. `test_freeze_guard_scenario.py` ×6 errors — `PermissionError WinError 32` teardown (all assertions pass) — environmental.
13. **`test_inv35_stuck_dust_no_exit.py` collection error** — hardcodes wrong prod path (`c:\Users\Gionie\...`), triggers isolation guard. Needs migration to `temp_db` fixture.
14. **Root-level scripts/archive tests** — `test_pragma_paths.py`, `scripts/audit_checkpoint_boolean_test.py`, etc. hit guard at collection. Move to `tests/` or mark `no_db`.

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

---

## Test Suite — Today's Results (Py3.11, 2026-09-20, final)

```bash
cd D:/Crypto_Quant_Bot && python -m pytest tests/ --ignore=tests/test_playwright_ui.py -q
# 698 collected, 0 collection errors in tests/
# 8 collection errors in archive/scripts root files (not under tests/)
```

**Collection errors (8) — NOT in tests/ directory:**
- `archive/tests/test_pragma_paths.py` — connects to live DB at import
- `scripts/audit_checkpoint_boolean_test.py` — connects to live DB at import
- `test_comprehensive_stress.py` — root file, import mismatch
- `test_mp_worker.py` — root file
- `test_pragma_paths.py` — root file, connects to live DB
- `test_stress_concurrent.py` — root file
- `engine/test_bot_engine.py` — NameError (missing import)
- `tests/test_playwright_ui.py` — missing playwright module

**Files triggering guard at collection (need migration to `temp_db` fixture or `no_db` mark):**
- `tests/test_inv35_stuck_dust_no_exit.py` (line 33: hardcoded wrong prod path)
- Root-level scripts and archive tests (not part of test suite proper)

---

## Git & Push Status

```bash
# All committed, NOT YET pushed to origin/main
git log --oneline -10
# 0d6f3c1 test: implement conftest-level DB isolation and write guard
# aac94fa fix(exchange): populate side and positionSide in testnet fetch_order wrapper
# f694edf docs: formalize non-negotiable rules 1-13 in AGENTS.md
# 9521d33 fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status...
# ad7e76e fix(database): recompute_invested_from_orders no longer excludes current-cycle fills...
# 76bcce9 fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills...
# 78f5600 fix(database): recompute_invested_from_orders no longer excludes current-cycle fills via wipe_wall_ts...
# 732db57 fix(database): sync_trades_from_orders adds is_active guard (8th site)
# 2b94ecb fix(bot_executor): _signal_hedge_child_entry adds is_active guard (7th site)
# 1e47869 docs: Option 1 single-writer consolidation complete...
```

---

## Next Blocker on Production Roadmap

**Step 6 — eliminate the 4 isolation artifacts (full-suite 4 → 0).** The residual `test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views` pass in isolation but fail under full-suite ordering due to shared-DB / Windows file-lock contamination. Fix: ensure each test uses an isolated temp/in-memory DB with properly closed connections in teardown (and the `test_freeze_guard_scenario` / `test_inv35_stuck_dust` collection errors get the same treatment). Goal: full-suite **0 failed / 0 hard-logic**, leaving only pre-existing Windows-env teardown errors.

**Additional:** Migrate `test_inv35_stuck_dust_no_exit.py` to `temp_db` fixture, move root-level scripts to `tests/` or mark `no_db`.

---

## Overnight / Unattended Safety

| Item | Status | Notes |
|------|--------|-------|
| Engine running? | **NO** | STOPPED — operator decision. Explicit go-ahead required. |
| Live positions at risk? | **NO** | Clean baseline; SOL −2.53 managed by bot 100001; all other pairs flat. |
| Circuit breaker healthy? | **YES** | Equity steady ~$9,117, no O-3/escalation at boot. |
| Barrier clean? | **YES** | 8/8 pairs verified in perfect parity. |

---

**End of 2026-09-20 session. Engine STOPPED. 3 commits local, push pending. Next session: Item 5 (audit_bot_wipes), Item 7 (retry-queue), Finding 2 parity_gates/database.py, Bot 10008/10018 orphan resolution, test_inv35 migration.**