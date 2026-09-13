# SESSION HANDOFF — 2026-09-13 (end of day)

**Session type:** Backfill corruption investigation → root-cause → systemic purge
**Repo state:** clean, HEAD = `39b800d` pushed to origin/main
**Engine state:** STOPPED (verified fresh at end of session — see last line)

---

## 1. THE BACKFILL CORRUPTION SAGA — FULL CHRONOLOGY

### 1a. Discovery: the $180M phantom BTC
Startup barrier blocked engine start with 8 "genuine anomaly" pairs.
Worst: **BTC/USDC:USDC ledger = -2332.192 BTC vs exchange +0.008** (~$180M phantom).

### 1b. Root cause (verified, not assumed)
- `_fetch_fills_for_bot()` in `engine/position_ledger.py` queried `exchange_fills` **by bot_id only — no symbol/pair filter**.
- Bot **100318** (configured `BTC/USDC:USDC` LONG) carried **5 backfill rows whose symbol was `SUI/USDC:USDC`** (SELL 1044.3, 1044.3, 7.3, 244.1; BUY 7.9 — ids 163–167, `source='backfill'`, cycle_id=157, step=1784245225 = bogus epoch-as-step).
- LONG bot + SELL fills → net −2332.192 "BTC" that was actually misattributed SUI quantities.
- **Legacy system was correct**: `get_pair_virtual_net('BTC/USDC:USDC')` = 0.0. The bug was introduced by the Phase 3/4/5 `exchange_fills` migration, only surfaced once position_ledger became the primary audit source.
- Blast-radius scan: only bot 100318 had cross-pair contamination (plus one trivially-netting BTCUSDC fill on test bot 1001, handled by normalized matching).

### 1c. Fix (both approved options)
- **Option A (query fix):** `_fetch_fills_for_bot()` now filters fills by the bot's configured pair (exact match OR `normalize_symbol()` UDF match; UDF registered per-connection inside the function). `_get_bot_config()` extended to return `normalized_pair`.
- **Option B (data fix):** deleted the 5 corrupt rows (ids 163–167).
- **Regression test:** `tests/test_cross_pair_fill_attribution.py` — 6 tests, RED on old query shape / GREEN with filter. Replay-exact scenario for bot 100318 included.
- Committed: `39b800d` "fix: cross-pair fill attribution bug (Option A + B) + cleanup" — **pushed to origin/main** (`37a95e2..39b800d` confirmed in push output).

### 1d. BTCUSDT test-bot garbage (next-largest anomaly, $10,745)
- Bots 999 / 1001 / 2001 = TEST_BOT / TEST_LIVE / FALLBACK_TEST — pure test fixtures living in the production DB.
- Worst fill: bot 999 id=25 `BUY 0.12 BTC @ $78.2` (BOT price, `source='backfill'`).
- Full-table impossible-price scan (BTC<$1000, ETH<$10, SOL<$1, BNB<$10, SUI<$0.05, XAU<$10, LINK<$0.5): **exactly one hit** (the $78.2 row). No zero/negative prices anywhere.
- John's call: this is a **test-isolation problem, not a delete-these-rows problem** — test bots have no business sharing the live DB/`exchange_fills` table with real bots.
- Deleted all 9 fills of the 6 original test bots; the 19 other test bots (TEST/USDC pair + numeric-name hedge test bots) had 0 fills.

### 1e. BTC/USDC:USDC residual −0.092 discrepancy → system-wide purge decision
- Per-bot: 10016 +0.154, 10022 −0.006, 100317 −0.240 — **all from `source='backfill'` rows** (60/2/7 fills).
- 100317 is configured LONG but backfill rows net it SHORT — backfill missed final flatten fills.
- Exchange truth: +0.008 BTC account-level, CID-verified net 0.000.

### 1f. THE QUESTION A VERDICT (the one that mattered)
**Was the corruption introduced by one-time backfill, or is the live dual-write path still producing it?**

Evidence from full `exchange_fills` source audit (raw query, not memory):

| source | rows | meaning |
|---|---|---|
| `backfill` | **402** | one-time migration — **ALL corruption lived here** |
| `test` | 1 | bot 1 fixture |
| `ws_live` / `stale_sync` / `reconciler` | **0** | live write paths had **never written a single row** |

Every corrupted row found all day (SUI phantom, $78.2 price, BTC/USDC −0.092) carried `source='backfill'`. **Verdict: 100% confined to backfill. The Phase 1 live-write architecture is sound — it simply had not yet produced data. The one-time migration was bad and is now removed.**

### 1g. System-wide purge + rebuild
- **Deleted all 402 `source='backfill'` rows** (BNB 68, BTC 69, ETH 43, LINK 9, SOL 89, SUI 75, XAU 45, XRP 4). Only the 1 `test` row remains.
- **Rebuild attempt:** `StateReconciler.reconstruct_offline_fills(since_hours=24*120)` → **0 fills reconstructed**. Exchange order history (120 days) contains no fills attributable to active bot CIDs. That is the exchange's own answer: current exchange positions were not opened by traceable CID orders.
- Engine re-run after purge: blocking anomalies went **8 → 4 → 1**.

---

## 2. FINAL PAIR-PARITY TABLE (post-cleanup, exactly as measured)

```
Pair                 ledger     legacy     fills   exchange
BNB/USDC:USDC       0.000000   0.000000       0   0.000  OK
BNBUSDT             0.000000   0.000000       0   0.000  OK
BTC/USDC:USDC       0.000000   0.000000       0   0.008  BLOCKER (orphan $614)
BTCUSDT             0.000000   0.000000       0   0.000  OK
ETH/USDC:USDC       0.000000   0.000000       0   0.000  OK
ETHUSDT             0.000000   0.000000       0   0.000  OK
LINK/USDC:USDC      0.000000   0.000000       0   0.000  OK
SOL/USDC:USDC       0.000000   0.000000       0   0.000  OK
SOLUSDT             0.000000   0.000000       0   0.000  OK
SUI/USDC:USDC       0.000000 118.700000       0 118.700  isolated $85, non-blocking
XAU/USDT:USDT       0.000000   0.000000       0   0.000  OK
XRP/USDC:USDC       0.000000   0.000000       0   0.000  OK
```

Engine log lines proving the above (from `/tmp/engine_post_backfill_purge.log`):
```
[STARTUP-ISOLATION] Mismatch on SUI/USDC:USDC is within threshold ($84.54 <= $100.00).
    Isolating problem by gating only bots: [10018]. Bypassing global block.
[STARTUP-BARRIER-FAIL] BTC/USDC:USDC: ledger=0.000000 exchange=0.008000 delta=0.008000
[STARTUP-BARRIER] BTC/USDC:USDC: GENUINE ANOMALY — unexplained_orphan=True,
    cid_explainable=False. Blocking startup.
FATAL: Startup parity verification FAILED for 1 genuine-anomaly pair(s).
```

Test suite at HEAD: **51/51 core tests pass** (auto_repair_guards 7, adopt_fill_guard 4, health_and_startup 24, position_ledger 7, deflate_a7 3, cross_pair_fill_attribution 6).

---

## 3. TWO DECISIONS AWAITING JOHN (flagged, untouched — do NOT action without his go)

### Decision A — BTC/USDC:USDC 0.008 BTC orphan (~$614) — THE LAST STARTUP BLOCKER
Real position on the exchange; zero CID-attributable fill history in 120 days. Above the $100 threshold, so the engine stays blocked until resolved. Bots 10016/100317/100318 + 12 numeric-name test hedge bots are plausibility-gated on this pair.

Options:
- **(a) Close the 0.008 BTC on the exchange** — exchange log shows `HYBRID RAW MODE ACTIVE (Demo FAPI)` → demo endpoint, no real capital. Cleanest, one manual click.
- **(b) Adopt to bot 10016** with a documented manual-proof fill (keeps the position, adds ledger row with `source='manual_proof'`).
- **(c) Add pair's bots to `STARTUP_EXCLUDED_BOT_IDS`** — unblocks engine, leaves position floating, weakest audit trail.

**Recommendation: (a) close on demo exchange.** It has no attributable owner, it's demo money, and starting the engine with an owned-again position matters less than an unambiguous zero. John deferred this to 2026-09-14 with a clear head — **explicitly NOT actioned tonight.**

### Decision B — SUI/USDC:USDC 118.7 SUI ghost (~$85) — NOT blocking, just noted
Real on exchange, matches legacy trades (118.7), **already whitelisted as a known migration-era orphan** (pre-existing operator note). Startup now auto-isolates it (gates bot 10018 only, value below $100 threshold). No action needed; it stays flagged until John decides to manually close or adopt. Note the **legacy `trades` table still carries the 118.7** — that column of the table above is the only remaining ledger/legacy divergence anywhere.

---

## 4. ARCHITECTURE MIGRATION — HONEST STATUS (correcting earlier overclaiming)

| Phase | Status | Evidence / caveat |
|---|---|---|
| **1 — Immutable fill log (`exchange_fills`)** | **DONE, schema + write paths verified** | Table exists, live paths (`credit_fill`, `sync_stale_open_orders`, reconciler) wired and unit-tested. NOTE: live paths have produced **zero rows so far** (engine never cycled live under this code) — first real cycle must be watched. |
| **2 — Canonical compute functions (`position_ledger.py`)** | **DONE** | `compute_bot_position` / `compute_pair_position` + 7 unit tests + 6 cross-pair regression tests all green at `39b800d`. |
| **3 — Shadow mode** | **DONE, verified on live pairs** | 2026-09-12 crosscheck: 30+ cycles, primary correct in every divergence (LINK/SUI/ETH/SOL traced to legacy bugs). The whole corruption saga today was shadow-vs-primary output doing its job. |
| **4 — Cutover of audit/health to primary** | **PARTIALLY DONE** | `audit_pair_ledger_vs_exchange` uses position_ledger as PRIMARY with legacy cross-check — VERIFIED by this session's logs. BUT: `get_pair_virtual_net` (legacy) is **still called at 9 production sites** (bot_executor, ground_truth_reconciler, integrity, oneway_netting, parity_gates, reconciler, wipe_proof, health, database). |
| **5 — Historical replay validation** | **NOT DONE** (PROJECT_STATUS 09-12 said IN PROGRESS; **09-13's PHASE_4_5_COMPLETION_REPORT.md OVERCLAIMED "complete" — treat that file as unreliable**) | The 4 incident replays (SUI over-sell 09-10, SOL downtime 09-09, ETH orphan 09-04, LINK freeze 09-08) were never actually run. AND moot for backfill-era incidents: the backfill rows they'd replay against no longer exist. Replay is now only meaningful against **post-purge live fills**. |
| **6 — Promote: swap remaining legacy call sites** | **NOT STARTED** | The 9 `get_pair_virtual_net` call sites above. Do NOT start until a live cycle has populated `exchange_fills` from real activity (otherwise those paths will read an empty table as truth). |

**Practical consequence:** with backfill purged, `exchange_fills` is empty of real history. Positions the engine owns going forward will be rebuilt from live fills; positions currently frozen REQUIRE_MANUAL_PROOF stay frozen until manually proven. That is the intended safety posture, not a bug.

---

## 5. OUTSTANDING VERIFICATION — DO NOT LET THIS DISAPPEAR

### ⚠️ The live 30–60 minute engine run with raw error log has NEVER been delivered.
Every "engine ran" today was a **~30–90 second startup-barrier run that exited FATAL by design** (or was truncated by timeout). What is still owed:
1. Resolve Decision A so the barrier passes.
2. Start `python engine/run_engine.py --no-trading` as a **background process with notify-on-complete**, run 30–60 minutes.
3. Capture the full log to a file, then deliver: count of ERROR/CRITICAL lines, categorized (`Raw API Error 400 -2013` stale-CID noise is expected background noise from test-bot rows still present in `bot_orders`), any NEW error class, and whether offline fill reconstruction writes live-sourced rows into `exchange_fills` (first-ever real output of the Phase 1 path — verify rows have `source='ws_live'`/`'stale_sync'`, correct symbol matching under the new pair filter).
4. Confirm the new `_fetch_fills_for_bot` pair filter does not wrongly exclude legitimately-filled live rows.

---

## 6. STANDING RULES FOR EVERY SESSION (restated)

- **Raw evidence per claim** — paste actual query/command output; "NOT FOUND" is a valid answer; never assert "fixed/verified" without it.
- **Two-gate deletes** — show exact rows → dry-run → confirm → execute. Any DB row deletion needs the row listing shown first, always.
- **Diff-and-test before applying** — regression test RED on old code, GREEN on new, before calling a fix done.
- **CRLF-safe edits** — Windows repo; this session hit f-string/backslash and patch-indentation failures; re-read file after every patch, verify with pytest (fresh process) not kernel memory.
- **One session only** — engine holds SocketLock on port 19888; verify no other instance before start (`netstat -ano | grep 19888`).
- **Stop on ambiguity** — financial-risk and money-touching calls (order placement/closure, DB writes to live positions) go to John, never auto-executed. REQUIRE_MANUAL_PROOF bots are never bulk-cleared.
- **Batch reports** — report on each pair resolution / new anomaly class / anything phantom-scale; otherwise continue autonomously.
- **Persist lessons into skills the same session** (standing user convention).

---

## 7. START HERE TOMORROW (priority order)

1. **Decision A (John):** close / adopt / exclude the BTC/USDC:USDC 0.008 orphan. Recommendation stands: close on demo FAPI. Show raw exchange position + exact command first.
2. **Engine startup verification** — one run, expect barrier PASS (or SUI-isolation only). Paste raw tail of log.
3. **The owed 30–60 min `--no-trading` run** (see §5) with categorized raw error log. Specifically watch for first live `exchange_fills` rows and confirm the pair-filter fix behaves on live data.
4. **Commit the purge state** — the 402-row purge + 9 test fills + 5 corrupt rows happened in the DB file (not git); DB is backed up automatically at engine start (`backups/crypto_bot_backup_20260913_*.db`). Consider a `scripts/` note or migration documenting the deliberate purge so it's not mistaken for data loss.
5. **Phase 6 promotion plan** (only after 3 passes clean): enumerate the 9 `get_pair_virtual_net` call sites, migrate each with per-site test, retire legacy path last.
6. **Test-isolation fix (from John's insight):** design why/whether test bots should exist in the live DB at all — add `is_test` flag + filter in `compute_pair_position`/audit queries, or move the 25 test bots to a separate DB. Test-bot rows in `bot_orders` are also the source of the recurring `Order does not exist` (code -2013) noise in every startup log.
7. Fix remaining known-broken bits from earlier audit (low priority): missing `fetch_order_by_client_order_id` on `ExchangeInterface` (warning-spams startup), `run_startup_heal.py` false-green exit-0 behavior, `test_downtime_wedge_realpath` / `test_ghost_clearing` / `test_freeze_guard_scenario` / `test_inv18_stale_cancel` integration failures.
8. Optionally annotate `docs/PHASE_4_5_COMPLETION_REPORT.md` with a correction banner (it overclaims — see §4).

---

## 8. FILES TOUCHED THIS SESSION (all committed in `39b800d` unless DB-only)

- `engine/position_ledger.py` — pair filter in `_fetch_fills_for_bot`, `normalize_symbol` UDF, `_get_bot_config` returns `normalized_pair`
- `engine/health.py` — TTL cache restructure, hedge-child checks
- `engine/database.py` — audit cross-check logging
- `tests/test_cross_pair_fill_attribution.py` — NEW, 6 regression tests
- `tests/test_auto_repair_guards.py`, `tests/test_adopt_fill_guard.py` — mock/signature fixes
- `docs/ARCHITECTURE_v3.5.md`, `docs/CHANGELOG.md` — v5.4.0
- `docs/PHASE_4_5_COMPLETION_REPORT.md` — NEW (**see §4 reliability caveat**)
- 17 stale session/backup files deleted (session summaries, `.bak`/`.tmp`/`.backup` copies)
- **DB-only (not in git):** 402 backfill + 9 test-bot + 5 corrupt `exchange_fills` rows deleted; DB auto-backed up under `backups/` before each engine run.

---

**Engine state, verified fresh at end of session:** no `python.exe` processes in `tasklist`, nothing listening on port 19888 (SocketLock free) → **ENGINE STOPPED**. `git status --short` empty, `HEAD=39b800d` == `origin/main` → **REPO CLEAN AND RESUMABLE**.
