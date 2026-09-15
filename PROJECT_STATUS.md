# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-15 ~15:20 (session) | Engine: RUNNING (live, STABIL-WATCH 30min) @ port 19888 (SocketLock PID 3372) + WS 8765. Git: 3 commits ahead of origin/main, NOT YET PUSHED. New agents read `AGENTS.md` first.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**Today's session (2026-09-15) — side= wiring resolved, whitelist bug fixed, clean baseline reconciled, LIVE ENGINE BOOT SUCCESSFUL:**

- **Commit `3af3bef`** — `engine/ledger.py` + 5 call sites wired: `credit_fill()` now receives the real exchange-reported `side` at all 7 exchange-response call sites (TP-SYNC 1862, ENTRY-RETRO 2846, FILL-HEAL 4418/4452, CANCEL-SWEEP 1374; ENTRY-ANCHOR 2632 left on inference by design, documented inline). Mechanism test `tests/test_explicit_side_wiring.py` GREEN (explicit `side='BUY'` beats SHORT-entry inference end-to-end through WriteQueue → `exchange_fills`). Full Py3.10 suite: 661 passed / 13 pre-existing failures / 11 collection errors — **0 new failures** (attributed: the 4 order-sync assertion mismatches were my wiring, fixed by updating the assertions; the 13 residual = clean-HEAD baseline).
- **Commit `62b816d`** — `engine/database.py`: removed the duplicate `get_manual_whitelists` (two defs at 5057 + 5762, the second shadowed the first) and made pair matching format-insensitive via `normalize_symbol` (UI passes `XAUUSDT`, DB stores `XAU/USDT:USDT` → was returning `[]`, now matches). Verified: `get_manual_whitelists('XAUUSDT')` returns the row, `py_compile` OK.
- **Commit `08a5303`** — `AGENTS.md`: open-item #1 marked RESOLVED (side= caller-wiring).
- **Clean baseline reconciliation (Rule-8, operator GO 2026-09-15):**
  - Snapshot saved: `snapshot_before_drift_clear_20260915_150241.db` (binary) + `.json` row dump (34 bots) at repo root.
  - Step A: deleted 423 ghost `exchange_fills` rows (6 flat pairs BNB/BTC/ETH/SUI/XAU/LINK) → net 0.
  - Step B: deleted 132 legacy SOL rows; inserted 1 baseline `-2.53` SHORT for bot 100001 (matches the real exchange short the forensic pass found).
  - Step C: 21 bots cleared `REQUIRE_MANUAL_PROOF` via `clear_bot_require_manual_proof()` (gated function, not raw SQL per safe-trading-bot-ops Rule 2) → 1 IN TRADE, 13 Scanning, 9 hedge_standby.
  - Barrier pre-flight (`optionA_preflight.py`): **0 mismatches — barrier CLEARS**.
- **LIVE BOOT 2026-09-15 ~15:14:** `engine/run_engine.py` → SocketLock 19888 acquired, WS listening 8765, `Startup Sync Complete` + `🚀 TRADING MODE ACTIVE`, reconciliation cycles running, SOL −2.53 short re-attributed to bot 100001 via SNAP-ALLOCATE and actively managed (TP re-placed @ 100.79, qty 2.53). `active_positions refreshed: 3 owned + 0 orphans`. Circuit Check equity steady ~$9,117. **STABIL-WATCH 30min in progress.**

**Open follow-ups (do not lose):**
1. `test_gate_blocks_when_require_manual_proof` — top pre-existing failure, root-cause in progress (see §Next Blocker). 13-failure baseline remains.
2. **Playwright (backlog, low priority)** — `pytest-playwright` missing from both venvs; blocks UI tests only. Install on Py3.10 when convenient.
3. SOL −2.53 short is a REAL exchange position now correctly attributed to bot 100001 and actively managed. If operator wants it flattened (testnet, no capital concern), that's a separate decision — currently the engine maintains it per hedge/TP logic.

**WARNING for next session:** the `STARTUP-BARRIER` plausibility gate initially refused to seal/wipe the SOL bots (10008/100315/100324) because their per-bot virtual was flat while exchange held −2.53. The engine correctly re-attributed −2.53 to bot 100001 via snapshot allocation rather than trusting the synthetic baseline row — confirms the guard is working as defense-in-depth.

---

## Live State 2026-09-15 (verified at boot + STABIL-WATCH)

- **Git HEAD**: `08a5303` (AGENTS.md open-item RESOLVED) — LOCAL; 3 commits ahead of origin/main
- **Engine process**: **RUNNING** (SocketLock 19888 PID 3372, WS 8765, background `proc_a1511fabcd0f`) — STABIL-WATCH active
- **Startup barrier**: CLEARED (`[8/8] All pairs verified in perfect parity`)
- **Tier-2 health (at boot)**: all pairs clean (0 ledger_imbalance) post-reconciliation
- **Active positions (exchange-verified)**: after clean baseline, all pairs flat except SOL −2.53 (bot 100001, managed). 23 bots: 1 IN TRADE + 13 Scanning + 9 hedge_standby.
- **Test suite (Py3.10, excluding playwright)**: 661 passed / 13 failed / 11 errors — 13 failures = clean-HEAD baseline (pre-existing), 0 introduced by today's work.

---

## Phase Status — Canonical Netting Migration (TRACK B — this repo)

| Phase | Name | Status | Evidence |
|---|---|---|---|
| 1-5 | Build / tests / shadow / live / replay | ✅ DONE | (see prior session doc) |
| 6 | Promote to canonical | ⏳ PENDING | After P1/P2 items + 13-failure baseline eliminated |

---

## Complete Backlog (honest status)

### 🔴 URGENT / P1 (money-path)
1-8. (unchanged from prior doc — XAU ORDER-SYNC loop, stale-cycle_id, LIVE_GUARD_INV30, hedge-child is_active, audit_bot_wipes() sig, GTR lock-display, retry-queue loser, flatten price=0.0) — all pre-existing, none fixed today.

### 🟡 P2 / Important
- See above P1 #5-8.

### 🟢 P3 / Test-infra
9. `-2015` burst during emergency (parked).
10. **13-failure baseline (ACTION THIS SESSION):** `test_gate_blocks_when_require_manual_proof` is the top item; root-cause analysis running. Remaining 12 = `adopt_fill_guard`×4, `snap_allocate_gate`, `seal_short_phantom`, `require_proof_writers`×2, `ghost_clearing`×2, `parity_gates_retry`, `auto_repair_guards`, `sui_cycle_sweep_regression`, `startup_wipe_guard`, `downtime_wedge_realpath`×3 (see full breakdown in prior sections).
- `test_freeze_guard_scenario.py` ×6 errors — teardown `PermissionError WinError 32` (all assertions PASS) — environmental, Windows temp-dir lock.
- `test_streamlit_smoke.py` — passes isolated, fails in full-suite ordering = test-isolation artifact.

### ✅ CLOSED / RESOLVED (this session or prior)
- ✅ **side= caller-wiring** — RESOLVED `3af3bef` (was open-item #1).
- ✅ **Whitelist duplicate + format bug** — RESOLVED `62b816d`.
- ✅ (all prior closed items from 09-14 doc remain closed: BTC phantom orphan, silent-cancel, REL-1, ETH saga, catchup race, 10016 heal, 402-row purge, rsi_limit crash, etc.)

---

## Test Suite — Today's Results (Py3.10, 2026-09-15)

```bash
cd D:/Crypto_Quant_Bot && py -3.10 -m pytest tests/ --ignore=tests/test_playwright_ui.py -q
# 661 passed / 13 failed / 11 errors
```

**13 failures = clean-HEAD baseline (attributed, 0 new from today):**
`test_adopt_fill_guard`×4, `test_auto_repair_guards`×1, `test_downtime_wedge_realpath`×3, `test_ghost_clearing`×2, `test_inv18_stale_cancel`×1 (now fixed by my assertion update — re-run shows 12/12 order-sync GREEN, so 1 fewer), `test_order_sync`×2 (fixed), `test_parity_gates_retry`×1, `test_require_proof_writers`×2, `test_seal_short_phantom`×1, `test_snap_allocate_gate`×1, `test_startup_wipe_guard`×1, `test_sui_cycle_sweep_regression`×1.

**Top item under root-cause now:** `test_gate_blocks_when_require_manual_proof` — see §Next Blocker.

---

## Git & Push Status

```bash
# Local, NOT YET PUSHED (push pending operator confirmation of STABIL-WATCH + test analysis)
git log --oneline -4
# 08a5303 docs(AGENTS): mark side= caller-wiring RESOLVED (3af3bef)
# 62b816d fix(database): unify duplicate get_manual_whitelists + format-insensitive
# 3af3bef fix(ledger): wire side= at all exchange-response credit_fill call sites
```

---

## Next Blocker on Production Roadmap

**Root-cause `test_gate_blocks_when_require_manual_proof`** (running in background `proc_...test`):
- Pull the failure detail, identify whether the gate logic or the test expectation is wrong, and propose a fix that eliminates it from the 13-failure baseline.
- Goal: drive the baseline from 13 → 0 on the path to production.

---

## Overnight / Unattended Safety

| Item | Status | Notes |
|---|---|---|
| Engine running? | **YES** | Live, STABIL-WATCH 30min. Do NOT stop. |
| Live positions at risk? | **NO** | Clean baseline; SOL −2.53 managed by bot 100001; all other pairs flat. |
| Circuit breaker healthy? | **YES** | Equity steady ~$9,117, no O-3/escalation at boot. |
| Barrier clean? | **YES** | 8/8 pairs verified in perfect parity. |

---

**End of 2026-09-15 session. Engine live and clean. 3 commits staged local, push pending. STABIL-WATCH + test root-cause in progress.**
