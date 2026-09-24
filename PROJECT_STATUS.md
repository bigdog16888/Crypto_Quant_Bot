# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-24 ~04:30 (NIGHT 2 autonomous window) | Engine: STOPPED (untouched all night). Git: HEAD `953de15`, 63 commits ahead of origin/main. New agents read `AGENTS.md` first. Full overnight detail: `OVERNIGHT_REPORT_NIGHT2.md`.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**NIGHT 2 (2026-09-23 → 09-24) — 2 of 3 tasks complete, 1 blocked on wrong premise:**

- **Commit `9ba4cad`** — Task 1 ✅: UI two-tier status. Top ribbon = tier-1 live exchange parity (`🟢 HEALTHY`); tier-2 ledger residue demoted to `ℹ️ LEDGER ARCHIVE ADVISORY` caption. No more false `🔴 MISMATCH` panic on dormant-bot ledger dust.
- **Commit `953de15`** — Task 3 ✅: GTR lock indicator in Live Monitor header (`🟢 ACTIVE` / `🔴 LOCKED` / `⚪ DISENGAGED`), derived from `is_engine_running()` + health `manual_proof_bots`/`stuck_cascade_bots`.
- **Task 2 🛑 BLOCKED — premise disproven by evidence**: tier-2 `LEDGER_ADVISORY` imbalance is computed from **`exchange_fills`** (immutable fill log) via `compute_bot_position()`, NOT `bot_orders`. There are **zero unowned `bot_orders` rows** to archive. Archiving `bot_orders` would change the tier-2 number by nothing; making it zero would require mutating `exchange_fills` — forbidden without operator sign-off and the wrong remedy (the residue is real closed history, not phantom). **Correct fix (designed, NOT applied):** dormant-bots exclusion gate in `engine/health.py` (all bots `is_active=0` + physical=0 ⇒ informational, not imbalance). Needs diff + approval.
- **NEW FINDING**: SUIUSDC is a health blind spot (all 3 SUI bots `is_active=0` + `open_qty=0` → excluded from tier-2 scan despite 80+ fills) AND forward-test grid order `185035956` (11.8 SUI BUY) is `filled` on exchange but has **no `exchange_fills` row** and a stale `open` `bot_orders` row. Exchange is flat — no live risk — but the ledger over-states SUI history. Decision needed morning (see open items).
- **Test suite: 703 passed, 0 failed, 2 skipped, 0 ERRORS** (fresh run at `953de15`).
- **Engine: STOPPED. Exchange: flat, no resting orders. UI: localhost:8501 read-only, live-verified via `scripts/tools/inspect_live_ui.py`.**

- **Commit `34cb543`** — Phase 3.1: XAU ORDER-SYNC loop fixed. Stranded partial fills (0.017 qty) credited on `-2011` errors; order cancelled cleanly.
- **Commit `6e5749d`** — Phase 3.2-3.3: SUI/USDC orphan flattened (58.6 contracts, Order ID 184956197). XAU virtual drift (-1.014) healed via direction-aware cycle_floor auto-detection + Mechanism B restoration.
- **Commit `d1a50a0`** — Phase 4: 15-minute dry-run soak test PASSED (148 cycles, 0 crashes, 0 parity drift, clean SIGTERM shutdown).
- **Current HEAD**: `d1a50a0` (52 commits ahead of origin/main)
- **Working tree**: CLEAN
- **Engine**: STOPPED (operator decision — explicit go-ahead required to start)
- **Testnet bots 10008/10018**: PAUSED, `is_active=0`, `status=STOPPED`, orphan exchange positions CLEARED (SOL 0.23 dust handled, SUI 58.6 flattened)
- **DB isolation guard**: ACTIVE — verified write blocked, read-only passes, temp DB works
- **Test suite**: **703 passed, 0 failed, 2 skipped, 0 ERRORS** (100% GREEN)

---

## Dated reminders (authoritative)

- 2026-07-21: tencent-hy3-free model retired — CONFIRM it is NOT in active config/routing
- 2026-07-28: laguna-m.1 model retired — IS delegation, benchmark replacement before removing
- 2026-09-20: rotate Rule-8 DB snapshots older than 30d (free disk, keep last 3 per milestone) [DONE:2026-09-20]

---

## Live State 2026-09-23 (verified at session end, engine STOPPED)

- **Git HEAD**: `d1a50a0` — LOCAL; 52 commits ahead of origin/main
- **Engine process**: **STOPPED** (operator decision — explicit go-ahead required to start)
- **Startup barrier**: CLEARED (all pairs verified in perfect parity)
- **Tier-2 health (at boot)**: all pairs clean (0 ledger_imbalance) post-reconciliation
- **Active positions (exchange-verified)**: after clean baseline, all pairs FLAT. SOL 0.23 dust handled via drift_note. XAU virtual=0.0 physical=0.0. BNB/BTC/SUI flat.
- **Bots 10008 (SOL), 10018 (SUI)**: `is_active=0`, `status=STOPPED`, orphan exchange positions CLEARED
- **Test suite (Py3.11, excluding playwright)**: **703 passed, 0 failed, 2 skipped, 0 ERRORS** (100% GREEN)
- **DB isolation guard**: VERIFIED — live write blocked, mode=ro passes, temp DB works

---

## 2026-09-23 Session Summary (this session)

### Commits this session (in order)

| Hash | Message | Type |
|------|---------|------|
| `9ba4cad` | fix(ui): separate tier-1 live exchange parity from tier-2 historical ledger advisory | Code |
| `953de15` | feat(ui): add GTR lock status indicator to live monitor | Code |
| `34cb543` | fix(executor): credit stranded partial fills on terminal stale order purge | Code |
| `34a3a34` | fix(ledger): harden handle_flatten price fallback chain against 0.0 exit prices | Code |
| `68aa8bd` | fix(reconciler): resolve audit_bot_wipes signature mismatch with cursor adapter | Code + Test |
| `6e5749d` | docs: bank overnight mission report and updated position audit | Docs |
| `d1a50a0` | fix(health): make cycle_floor auto-detection direction-aware for SHORT bots | Code |

### Previous session commits (preserved)

| Hash | Message | Type |
|------|---------|------|
| `ffbda87` | fix(suite): achieve 100% pass rate (698 passed) - resolve Clusters A-E and config bleed | Test |
| `eb4a692` | gitignore: ignore snapshots/ directory | Config |
| `21f2902` | scripts: track forensic audit and diagnostic scripts | Scripts |
| `8b15fdd` | docs: organize root investigation memos into docs/investigations/2026-09-18_offline | Docs |
| `a64a591` | test: make test_freeze_guard_scenario teardown Windows-safe against file locks | Test Infra |
| `34f43fa` | test: track verified regression tests (a1a2_real_fill, sui_cycle25, compute_position_state) | Test |
| `3b39cef` | test: migrate cross_pair_fill_attribution and reconciler_cid_parsing to temp_db fixture with exchange_fills schema | Test |
| `55497ff` | test: update line numbers and add startup quarantine to writer proof whitelist | Test |
| `0d6f3c1` | test: implement conftest-level DB isolation and write guard | Test Infra |
| `aac94fa` | fix(exchange): populate side and positionSide in testnet fetch_order wrapper | Code |
| `f694edf` | docs: formalize non-negotiable rules 1-13 in AGENTS.md | Docs |
| `9521d33` | fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status... | Code + Test |
| `ad7e76e` | fix(database): recompute_invested_from_orders no longer excludes current-cycle fills... | Code |
| `76bcce9` | fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills... | Code |
| `78f5600` | fix(database): recompute_invested_from_orders no longer excludes current-cycle fills via wipe_wall_ts... | Code |
| `732db57` | fix(database): sync_trades_from_orders adds is_active guard (8th site) | Code |

---

## Phase Status — Canonical Netting Migration (TRACK B — this repo)

| Phase | Name | Status | Evidence |
|-------|------|--------|----------|
| 1-5 | Build / tests / shadow / live / replay | ✅ DONE | (see prior session doc) |
| 6 | Promote to canonical | ⏳ PENDING | After P1/P2 items + isolation baseline eliminated |

---

## Complete Backlog (honest status)

### 🔴 URGENT / P1 (money-path)

1. **Finding 2 — Side inference gap in parity_gates/database.py** — **RESOLVED 2026-09-24**. Both sites already pass `side=`:
   - `parity_gates.py:1142` → `side=o.get('side', '')`
   - `database.py:2182` → `side=_detail.get('side', '')`
   No diff needed. Verified at `0bffdde`.

### 🟡 P2 / Important

- **Task 2 remnant — tier-2 dormant-bots gate (NIGHT 2, designed not applied)**: `engine/health.py` should not flag `ledger_imbalance` when ALL bots on a pair are `is_active=0` AND exchange physical=0 (residue is provably closed history). Active-bot pairs keep strict tier-2. **Diff ready for approval** (see HANDOFF).
- **SUI blind spot + uncredited fill (NIGHT 2)**: all-dormant SUIUSDC pair excluded from tier-2 scan (health.py:165-168) despite 80+ fills; grid order `185035956` (11.8 SUI BUY, filled on exchange) has no `exchange_fills` row and stale `open` `bot_orders` row. **Exchange flat (0.0)** — SELL fills (orders 184956197, 185019137) offset all BUYs. Correct path: `reconstruct_offline_fills(pair_filter='SUIUSDC')` to credit BOTH BUY and SELL fills → net flat. Decision needed.

### 🟢 P3 / Test-infra

1. **Failure baseline**: 0 logic failures remain. All 703 tests pass.
2. **Repo hygiene**: ~34 untracked scratch files at repo root (`check_*.py`, `debug_*.py`, …) — belong in `%LOCALAPPDATA%\Temp`; deletion needs approval. `AGENTS.md` working tree carries a stale stage header (docs-only).

---

## Test Suite — NIGHT 2 Results (Py3.11, 2026-09-24, at `953de15`)

```bash
cd D:/Crypto_Quant_Bot && python -m pytest tests/ --ignore=tests/test_playwright_ui.py -q --no-header
# raw:
703 passed, 2 skipped, 10 warnings, 4 subtests passed in 126.20s (0:02:06)
```

**703 passed, 0 failed, 2 skipped, 0 ERRORS (100% GREEN)** — run after both
NIGHT 2 UI commits (`9ba4cad`, `953de15`), so it covers them.

---

## Git & Push Status

```bash
# All committed, NOT YET pushed to origin/main
git log --oneline -8
# 953de15 feat(ui): add GTR lock status indicator to live monitor
# 9ba4cad fix(ui): separate tier-1 live exchange parity from tier-2 historical ledger advisory
# bf26cb2 fix(ui): eliminate 1hr exchange cache, bind refresh button to fragment state, purge stale whitelist; fix(health): balance display via proper .env loading in streamlit subprocess
# aca240c fix(ui): eliminate 1hr exchange cache, bind refresh button to fragment state, purge stale whitelist
# d1a50a0 fix(health): make cycle_floor auto-detection direction-aware for SHORT bots
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

**Finding 2 — Side inference gap in parity_gates/database.py (2 sites)** — The only remaining P1 item. Exchange-layer fix (`aac94fa`) resolved the testnet wrapper; these two internal sites still need explicit `side=` wiring.

**NIGHT 2 additions (P2, need approval before any diff):**
1. Tier-2 dormant-bots exclusion gate in `engine/health.py` (the only correct
   remedy for the `LEDGER_ADVISORY` dust — see `OVERNIGHT_REPORT_NIGHT2.md` §3).
2. SUI blind spot + uncredited fill `185035956` decision (a/b in report §4).

---

## Overnight / Unattended Safety

| Item | Status | Notes |
|------|--------|-------|
| Engine running? | **NO** | STOPPED all night (Rule 2). Explicit go-ahead required. |
| Live positions at risk? | **NO** | Full `fetch_positions()` scan: zero non-zero positions. SUI TP already canceled on exchange; no resting orders. |
| Circuit breaker healthy? | **YES** | Equity ~$9,104.95 (testnet), steady. |
| Barrier clean? | **YES** | Tier-1 parity: worst_gap_usd = 0.0, 0 mismatched pairs. |
| DB writes this night? | **NO** | All queries `mode=ro`. Zero mutations. |
| Orders placed/canceled? | **NO** | None. |

---

**End of NIGHT 2 (2026-09-24 ~04:30). Engine STOPPED. 2 code commits this window (`9ba4cad`, `953de15`) + docs. Next session: Task 2 tier-2 dormant-bots gate (diff + approval), SUI decision, Finding 2 side= wiring.**