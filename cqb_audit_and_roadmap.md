# Crypto Quant Bot Production Audit and Roadmap — v2 (rework)

- **Generated**: 2026-09-07 (Taipei, GMT+8) by studio-builder-a (z-ai/glm-5.3-free via tokenrouter)
- **Measured at**: git HEAD `0531b2cb21a480b1bbcafe46facea0c78afd8d8f` (2026-09-04 13:48:30 +0800, detached HEAD)
- **Supersedes**: v1 (2026-08-07) — REJECTED by review `t_08f125ae` ("4 fabricated/false figures + 2 false gap claims + false health.py mutation claim")
- **Rework scope**: this file only. The v1 doc's un-executed `$(date +%Y-%m-%d)` template bug (v1 line 3) is gone; every figure below was re-measured from the repository, not carried over.

**Context at measurement time**: the trading engine is LIVE (engine.log mtime 2026-09-07 11:50, actively written; last_shutdown.ts = 2026-09-04 14:16:30 +0800). All analysis below was read-only (git ls-tree/grep/show, schtasks /query, AST parse, pytest --collect-only). No engine code, scripts, DB, or exchange endpoints were executed.

---

## 0. Reproducibility contract

Every figure in this document comes from the table below (or a command cited inline). All commands ran in the repo root at HEAD `0531b2c` unless another hash is stated. Audit-time comparison commit: `6a92f3a` (2026-07-30 16:37:41 +0800) — the closest commit to when v1 was written (2026-08-07).

| Figure | Value | Command |
|---|---|---|
| HEAD commit | `0531b2c` (2026-09-04) | `git rev-parse HEAD` |
| Version | v5.3.7 (GUIDE §8 latest entry, 2026-07-10) | `git show HEAD:CODEBASE_GUIDE.md` |
| Tracked .py files | **187** (at audit-time `6a92f3a`: 156) | `git ls-tree -r HEAD --name-only \| grep -c '\.py$'` |
| On-disk .py files | **212** = 187 tracked + 23 untracked-unignored + 2 gitignored (scratch/) | `find . -name '*.py' -not -path './.git/*' \| wc -l` + `git status --short` |
| Collected tests (live) | **610 collected, 0 errors** (pytest 2026-09-07, Py3.10 venv) | `pytest --collect-only -q tests/` |
| Tracked `def test_` in tests/*.py | **604** at HEAD; **466 in 81 files** at `6a92f3a` | `git grep -o "def test_" HEAD -- tests/ \| wc -l` |
| tests/*.py files | 104 tracked, 105 on disk (1 untracked: test_catchup_fill_race_replay.py, 6 tests) | `git ls-tree -r HEAD --name-only tests/ \| grep -c '\.py$'` |
| engine/ .py files | 34 top-level + 5 runner/ + 4 strategies/ + 13 migrations/ = **56**, 36,564 LOC | `git ls-tree -r HEAD --name-only engine/ \| grep '\.py$' \| xargs wc -l` |
| Functions (AST, engine/) | 561 total; **71 >100 lines**; 393 (70%) type-annotated | AST walk of engine/*.py (parse failures: 0) |
| camelCase defs | 4 — `iATR`, `iATRPercentile`, `iRSI` (TA-indicator names, intentional) | AST walk |
| WriteQueue call sites | **66** `WriteQueue().put_and_wait` across 9 engine modules | `git grep -c "WriteQueue().put_and_wait" HEAD -- 'engine/*.py'` |
| INV-39/40/41 mentions | **0 each** — these INV numbers do not exist anywhere in the repo | `git grep -cE "INV-39" HEAD` (same for 40, 41) |
| AGENTS.md at repo root | **does not exist** | `git ls-tree -r HEAD --name-only \| grep -i agents` → empty |

Reviewer's "650 `def test_` across 109 files today" (review §2) is a whole-disk text grep (matches markdown docs, scratch logs, and .diff files too — the same method yields 732 occurrences across 117 files at HEAD). The canonical live figure is pytest collection: **610 tests, 0 collection errors**.

---

## 1. Executive summary (corrected)

CQB is a Binance Futures USDC one-way-mode grid/martingale engine (v5.3.7, 56 engine modules / 36.6k LOC, 187 tracked Python files, 610 collected tests) with a proof-only reconciliation architecture and a WriteQueue-serialized write path (INV-31). The v1 audit's headline numbers (169 files, 87 tests) were fabricated — nothing in the repo supports them, including at the audit-time commit (156 files, 466 tests).

The system is in materially better shape than v1 claimed: log rotation exists and demonstrably works; Prometheus metrics export exists; scheduled operational checks exist; backup and restore tooling exists; the massive INV-31 WriteQueue refactor that v1 prescribed as future work was already executed on this board (≈64 tasks, commits `46ea625`, `f25fbce`, `ed1d8cd`), migrating the worst violators.

What is ACTUALLY wrong, measured at HEAD:

1. **A live Windows scheduled task has been failing every minute since 2026-07-23** — `CryptoBot_Heartbeat_Monitor` targets `scripts/heartbeat/cron_monitor.py`, which does not exist on disk and has never existed in git history (Last Result 2 = file-not-found).
2. **No alert delivery path exists** — Prometheus gauges are exported, but there is no consumer: zero Telegram/SMTP/webhook/notify code in engine/ or scripts/ (grep: 0 refs), no thresholds, no queue-depth/parity-gate/reconnect gauges.
3. **Specific zero-coverage public behaviors** — `_resolve_position_side_param` (RULE #0 one-way-mode guard, 6 call sites, 0 test refs), `execute_exit_tp`, `close_unattributed_position`, `diagnose_pair_orphans`, `attribute_anonymous_fill`, `mark_order_filled`, `MetricsServer` (all 0 test refs).
4. **INV-31 residuals outside the WQ convention** — 44 direct INSERT/UPDATE/DELETE statements on `trades`/`bot_orders` in 20 non-WQ-routed functions across 8 modules (details in §3.3), needing per-site adjudication (migrate or document exemption).
5. **Function-size hotspots** — `reconciler.resolve_net_mismatch` (2,962 lines), `bot_executor.maintain_orders` (2,229), `reconciler._reconstruct_offline_fills_internal` (2,006), `_adopt_from_physical_positions_internal` (1,055), `runner.cycle_loop.run_cycle` (706).
6. **Repo hygiene** — tracked backup junk (`engine/database.py.bak2`, `.tmp`, `tests/*.backup2/.backup3/.bak3/.before_fixture_change`, `engine/ledger.py.patch`, 4× `crypto_bot.db.backup_*`), dead `config/default_config.json` (referenced by zero tracked code; `METRICS_PORT: 9099` contradicts settings.py's 9090), stale `requirements.txt` header ("v0.9.0" vs actual v5.3.7; pytest commented out).

---

## 2. Corrections to v1 (the review's 7 rework items)

| # | Review defect | Correction in this doc |
|---|---|---|
| 1 | Fabricated figures ("87 tests", "169 files") | §0 table: 610 collected / 604 tracked at HEAD; 466/81 at audit-time `6a92f3a`; 187 tracked .py (156 at audit time). v1 also claimed raw audit data was "4225 characters" — the actual file is 4,367 bytes. |
| 2 | False absence claims (log rotation, scheduled monitoring) | §3.1: rotation exists (`engine/runner/__init__.py:21,55`) and empirically works; two schtasks exist (one healthy, one broken — §3.5). What's actually missing is restated precisely. |
| 3 | health.py "state mutation" claim | Deleted. health.py: 568 lines, 13 `.execute(` — all SELECTs, 0 writes, 0 commits (`git grep -nE "INSERT\|UPDATE\|DELETE\|REPLACE" HEAD -- engine/health.py` → 0). It is imported by `ui/views/monitor.py:20` and `tests/test_health_and_startup.py`. |
| 4 | Coupling claims not tied to matrix; 13-vs-9 unreconciled | §3.3 uses `cqb_coupling_matrix.md` as the evidence base and reconciles 13-vs-9 with the INV-31 boundary definition. |
| 5 | No per-path test gap analysis | §4: named symbols with measured 0 test refs + WS fault-injection gaps (0 disconnect/out-of-order/chaos test files). The one valid v1 finding (`_resolve_position_side_param`) is retained and made a roadmap task (T1.4). |
| 6 | Effort-L items not session-scoped; priorities wrong after false premises | §5: every roadmap item is single-session; the two L refactor targets are decomposed into named per-cluster extraction tasks (Task-3 precedent: the INV-31 item became ≈64 board tasks). |
| 7 | "AGENTS.md" listed as documentation | §3.7: AGENTS.md does NOT exist at repo root at HEAD (nor anywhere tracked). Operating discipline lives in CODEBASE_GUIDE.md (root) + PROJECT_STATUS.md + docs/ runbooks/ADRs. |

---

## 3. Corrected findings

### 3.1 What v1 claimed missing but EXISTS (measured)

- **Log rotation — exists and works.** `engine/runner/__init__.py:21,55`: `RotatingFileHandler(engine.log, maxBytes=10*1024*1024, backupCount=5)`. Identical code existed at audit-time `6a92f3a` (same lines), so v1's "logs grow indefinitely" was false then and now. Empirical proof: `engine.log.1` … `engine.log.5` each ≈ 10,485,649 bytes on disk — rotation at exactly the configured 10 MB cap, 5 backups.
- **Structured logging — exists.** `logging` module with formatter `'%(asctime)s - %(name)s - %(levelname)s - %(message)s'`, file + stream handlers (`engine/runner/__init__.py:52-69`). The only `print(` calls in live engine code are in `bot_management.py:425-440` — all inside an `if __name__ == "__main__":` CLI block — and 2 migration CLI mains. (The other 12 `print(` hits at HEAD are inside tracked junk backup files: `database.py.bak2`, `database.py.tmp`.) Correlation IDs, however, genuinely do not exist (§5 T1.5).
- **Scheduled monitoring — exists.** Two Windows scheduled tasks, both Enabled/Ready (`schtasks /query`): `CQB_SessionStartCheck` (daily 09:05, Last Result **0** — healthy; runs tracked `scripts/session_start_check.py`, a read-only dated-reminder safety net) and `CryptoBot_Heartbeat_Monitor` (every 1 minute, Last Result **2** — broken, §3.5). The engine also self-reports a heartbeat log line every 60 s (`engine/run_engine.py:204-206`).
- **Health endpoint / metrics export — exists.** `engine/metrics.py`: Prometheus `MetricsServer` (thread) started by `run_engine.py:75` on `config.METRICS_PORT` (env default 9090, `config/settings.py:37`), exporting gauges: `bot_cycle_time_seconds`, `bot_active_count`, `bot_in_trade_count`, `account_equity_usd`, `account_drawdown_percent`, `bot_order_count_daily`, `bot_order_count_cycle`. Read-side system health: `engine/health.py` (read-only aggregation, consumed by the UI monitor view).
- **Backup and restore — exist.** `create_backup.bat` + `scripts/create_version_backup.ps1` + `docs/BACKUP.md` (zip with WAL-checkpointed DB + manifest); restore scripts tracked: `scripts/restore_db.py`, `restore_bots_table.py`, `full_restore_and_align.py` (+ `_do_not_run/` quarantined variants). v1's "No rollback procedures documented" was false.
- **Deployment automation — partially exists.** `Dockerfile` (python:3.10-slim) + `docker-compose.yml` (service `crypto-bot-ui`, port 8501, DB/.env/engine.log volume mounts, `restart: unless-stopped`) — but the container CMD runs the **Streamlit UI only**, not the trading engine (§3.6).
- **RULE #0 enforcement is code-backed, not just documentation.** One-way mode is implemented via positionSide suppression (`_resolve_position_side_param`, `engine/bot_executor.py:1049`) with 6 call sites (`bot_executor.py:1561, 3252, 3606, 3857`; `ledger.py:1501`); positionSide semantics are referenced by 4 test files (15 refs) and oneway netting has 4 dedicated test modules (`test_inv38_netting_aware_close.py`, `test_o2_unify_netting.py`, `test_oneway_repair_sign.py`, `test_ui_netting_suppression.py`). There is, however, no automated static guard (§5 T2.6) and no direct unit tests on the resolver itself (§4).

### 3.2 Test suite reality

- **610 tests collected, 0 errors** (live pytest collection, 2026-09-07). Tracked at HEAD: 604 `def test_` in `tests/*.py` (104 files); one untracked test file adds 6. At the audit-time commit `6a92f3a`: 466 tests in 81 files. Growth: +144 tracked tests since v1 was written.
- v1's "87 tests" matches nothing — not HEAD, not `6a92f3a`, not any board-known figure (the board already had a 582-collected count before v1 was written, per review §2).
- Coverage is deep where it matters most: `credit_fill` 138 refs/18 files, `seal_trade_state` 165/26, `safe_wipe_bot` 47/12, `resolve_net_mismatch` 23/6, `_place_gtx_order_with_retry` 27/4, `enforce_hedge_child_state` 24/1, partial-fill logic 16 files, hedge lifecycle has dedicated modules (`test_hedge_lifecycle.py`, `test_hedge_watchdog.py`, `test_hedge_loop.py`), WS health-check restart logic is tested (`test_ws_health_check.py`, 3 tests).
- The invariant set documented in CODEBASE_GUIDE §3 is **INV-17 … INV-38** (INV-42 appears only in the §8 changelog, GUIDE :1100). v1's "INV-29 through INV-42 well-defined" is wrong: INV-39/40/41 appear nowhere in the repo (grep count 0 each).

### 3.3 Coupling and INV-31 — evidence base and 13-vs-9 reconciliation

**Evidence base**: `cqb_coupling_matrix.md` (repo root, tracked) — the committed artifact of task `t_cd34b6f1` (2026-08-07, measured BEFORE the Group A refactor). Its auditable figures: 40 modules analyzed (31 top-level + 9 runner/strategies), 19 modules with direct DB access, 9 INV-31 violators with per-module counts (bot_executor 56, ground_truth_reconciler 4, oneway_netting 4, exchange_interface 3, parity_gates 3, recovery 3, ws_event_handlers 2, reconciler_wipe_audit 1, wipe_proof 1 — total 77 direct writes on trades/bot_orders).

**13-vs-9 reconciliation** (review defect 4): the `t_cd34b6f1` run *summary* claimed "13 modules violate INV-31 … 32 files … bot_executor 208 direct writes". That summary contradicts its own committed artifact on three counts: 13 vs 9 violators, 32 vs 40 analyzed modules, 208 vs 56 bot_executor writes. The INV-31 boundary (CODEBASE_GUIDE.md:38, §3.33 at :511) is: *"All database writes targeting the `trades` and `bot_orders` tables must run through the WriteQueue singleton … Direct conn.execute/commit calls for mutations are prohibited outside of the WriteQueue."* Under that boundary the artifact's **9** is the correct, reproducible count (the matrix lists exactly 9 modules with trades/bot_orders writes and no WriteQueue); the summary's 13 most plausibly counted modules with any direct DB write on any table (the matrix separately lists modules writing bots/active_positions etc.), and its 208/32 figures are not derivable from the artifact at all. **The artifact stands; the summary metadata does not.** (Same lesson as v1: run-summaries are claims, artifacts are evidence.)

**State at HEAD (after Group A)**: the board executed the matrix's fix list as its "Phase 1 Task 3" — Group A: 28 tasks (per review t_08f125ae), ≈64 including the bot_executor/oneway_netting/parity_gates/ws_event_handlers/wipe_proof/reconciler_wipe_audit/GTR continuation tasks; commits `46ea625`, `f25fbce`, `ed1d8cd` among others. Re-measured at HEAD:

- Fully migrated (matrix violator → 0 direct trades/bot_orders writes at HEAD): **ws_event_handlers** (now 7 `put_and_wait` sites), **reconciler_wipe_audit** (4 sites), **wipe_proof** (2 sites), **oneway_netting** (its 4 writes now live in module-level SQL constants consumed by `_wipe_bot_ghost_pre/post_seal_internal`, which are WQ-routed; the 5th site at :164 runs inside a WQ-internal per its docstring), **ground_truth_reconciler** (3 of 4 wrapped in `_heal_ghost_virtual_internal` via `WriteQueue().put_and_wait`; 1 remains in `run`).
- Remaining direct writes on trades/bot_orders **outside the WQ-routed `_internal` convention**, by enclosing function (AST-measured at HEAD):

| Module | Function | Writes | Note |
|---|---|---|---|
| bot_executor | `sync_stale_open_orders` (4), `execute_exit_tp` (4), `enforce_hedge_child_state` (3), `_reset_to_hedge_standby` (3), `execute_entry` (2), `_cancel_non_tp_orders` (1), `_compute_effective_tp` (1) | 18 | 34 other bot_executor write-clusters ARE WQ-routed (`_maintain_*_internal`, `_signal_hedge_child_entry` etc.) |
| ledger | `handle_tp_completion` (4), `mark_order_filled` (2), `handle_flatten` (2) | 8 | `ledger._credit_fill_internal`/`_seal_trade_state_internal` are WQ-routed (9 statements inside) |
| reconciler | `resolve_net_mismatch` (3), `_mark_order_filled` (1), `validate_individual_bots` (1), `heal_cycle_fragmentation` (1) | 6 | |
| runner.cycle_loop | `_handle_pending_flatten` (3), `_handle_pending_close` (1) | 4 | writes via passed-in `conn`; cycle_loop has no WQ import (only `flush()` at :336-337) |
| parity_gates | `deflate_pair_ledger_overcount` | 3 | dual-mode after B2 fix (commits `t_9aa16128`/`21f18001`) — takes caller `conn` |
| recovery | `resolve_gated_bot` | 3 | crash-recovery path; WAL receipt INSERT (:154) + status UPDATEs |
| exchange_interface | `create_order_with_receipt` | 1 | deliberate INV-16 pattern: synchronous pending-receipt INSERT on the caller's open transaction before the exchange call |
| ground_truth_reconciler | `run` | 1 | the 3 `_heal_ghost_virtual_internal` writes are WQ-routed |

  Excluded from "residual" by design: `engine/database.py` (43 non-WQ statements — it is the DB access layer that the WQ internals themselves call; matrix note 1: "database.py owns the WriteQueue singleton … its direct writes are compliant") and `migrations/migration_002` (runs at startup, before the WQ exists).
  *Caveat: "outside the convention" means the statement sits in a function whose name is not a WQ-routed `_internal` body; whether a given call actually executes off the WQ worker thread depends on the caller — that is exactly what T2.5 adjudicates per site. Total residual: **44 statements in 20 functions across 8 modules**.

- Import-coupling hotspots (from the matrix, unchanged in kind at HEAD): `database` and `exchange_interface` are the central hubs (database imported by 22 engine modules; exchange_interface by 20). bot_executor imports 10 engine modules.

### 3.4 Code quality (AST-measured, engine/ at HEAD)

- 561 functions; 393 (70%) annotated; 4 camelCase defs (TA-indicator names — intentional, not an inconsistency); 1 TODO in engine/ (`manager.py:336` — "Implement actual market liquidation logic"; the emergency-liquidation path that exists is `tests/test_emergency_liquidation.py`-covered, but manager-level liquidation is a stub).
- 71 functions >100 lines. Worst: `reconciler.resolve_net_mismatch` **2,962**; `bot_executor.maintain_orders` **2,229**; `reconciler._reconstruct_offline_fills_internal` **2,006**; `reconciler._adopt_from_physical_positions_internal` **1,055**; `runner.cycle_loop.run_cycle` **706**; `bot_executor.process_bot` 416; `execute_entry` 419; `_signal_hedge_child_entry` 372; `ledger.handle_tp_completion` 370; `database._reset_bot_after_tp_internal` 328.
- Configuration is NOT "scattered across multiple files" as v1 claimed: runtime config is centralized in `config/settings.py` (env-overridable: `PAIR_NETTING_TOLERANCE`, `HEDGE_LIVE_GUARD_*`, drawdown breaker, etc.) + `config/constants.py`. `config/default_config.json` (41 lines) is referenced by **zero** tracked files and contradicts settings.py (`METRICS_PORT: 9099` vs env default 9090) — dead config, not evidence of scatter.

### 3.5 Observability: actual state vs actual gaps

EXISTS: rotating structured logs (§3.1); Prometheus MetricsServer with 7 gauges (§3.1); engine 60 s heartbeat log line; daily session-start schtask (healthy); in-DB operational audit trails (`exchange_order_audit`, `reconciler_wipe_audit`, `wipe_proof` modules).

ACTUALLY MISSING (v1's real gaps, restated precisely):

1. **`CryptoBot_Heartbeat_Monitor` is live-but-broken**: fires every 1 minute since 2026-07-23 12:20 against `scripts\heartbeat\cron_monitor.py --threshold 120`, which does not exist (not on disk; `git log --all --oneline -- '*cron_monitor*'` → empty — it was never committed). Every execution fails (Last Result 2). This is a silent dead monitor pretending to watch the system.
2. **No operational gauges**: nothing exports WriteQueue queue depth, parity-gate trips (REQUIRE_MANUAL_PROOF transitions), or WS reconnect counts — the three failure signals the architecture actually cares about. `metrics.py` covers equity/drawdown/order counts only.
3. **No alert delivery**: zero Telegram/SMTP/webhook/notification code in engine/ or scripts/ (grep: 0 refs). Thresholds are defined nowhere. The drawdown gauge exists but nothing consumes it.
4. **No MetricsServer tests** (0 test refs) — a silently dead metrics thread would go unnoticed.
5. **On-call / incident response**: for this single-operator system the on-call equivalent is incident runbooks — which DO exist (`docs/OPERATOR_MISMATCH_RUNBOOK.md`, CODEBASE_GUIDE §7 restart-safety, `docs/BACKUP.md` restore flow, `docs/DAILY_SHUTDOWN_SELF_HEALING.md`). What is missing is the notification leg: an incident (REQUIRE_MANUAL_PROOF gate, freeze, watchdog trip) currently surfaces only when the operator opens the UI — that is T1.3's alert delivery, not a runbook gap.

### 3.6 Deployment and rollback reality

- Windows host path (current production): `run_stack.bat` (engine `python engine\run_engine.py` + 5 s wait + Streamlit UI) — engine and UI are separate processes; `restart_runner.bat`, `run_bot.bat` (UI only). The engine is running from this path now (§ context).
- Container path: `Dockerfile`/`docker-compose.yml` containerize the **UI only** (CMD = `streamlit run ui/app.py`). The trading engine has no container entrypoint, no compose service, no healthcheck. v1's "Package as Docker container" task is half-done already — the gap is the ENGINE side only.
- Rollback: manual procedures documented (`docs/BACKUP.md`) + tracked restore scripts + 4 tracked DB backups. Missing: a single orchestrated backup→verify→restore→startup-barrier-check command, and health-gated promotion between versions.
- No CI config, no `.pre-commit-config.yaml`, no pytest.ini/pyproject/setup.cfg exist at HEAD (verified) — all gates are currently manual.

### 3.7 Documentation reality (review defect 7)

**AGENTS.md does NOT exist at repo root at HEAD** — nor anywhere tracked (`git ls-tree -r HEAD | grep -i agents` → empty). Two commits that author/move an AGENTS.md exist in the object database (`673e707` 2026-07-17, `a389e58` 2026-07-28) but `git merge-base --is-ancestor` confirms **neither is an ancestor of HEAD** — they live on side branches (e.g. `fix/adr010-tp-cascade-persistence`) and never landed on this line. The task context that assumed an AGENTS.md was describing a branch that was never merged here.

At THIS HEAD, operating discipline lives in:

- `CODEBASE_GUIDE.md` (repo root, 2,086 lines) — authoritative: RULE #0 one-way mode (:53), Critical Architectural Invariants §3 (:239), INV-31 write serialization §3.33 (:511), restart safety §7, version history §8, RULE-ENV .env policy (:2006), reconciliation & virtual hedging §11.
- `PROJECT_STATUS.md` — header rules ("Read this FIRST … NEVER mark anything done without proof-before-claiming evidence").
- `docs/OPERATOR_MISMATCH_RUNBOOK.md`, `docs/adr/*` (ADRs 002-011), `docs/CHANGELOG.md` (not root), targeted root-cause docs (e.g. `docs/REL1_EMERGENCY_PATH_ROOT_CAUSE_20260904.md`).

---

## 4. Per-path test gap analysis (measured at HEAD)

All "0 refs" below = `git grep -c "<symbol>" HEAD -- tests/` returned nothing (verified 2026-09-07).

**Order execution paths:**
- `_resolve_position_side_param` (`bot_executor.py:1049`, RULE #0 positionSide resolver) — **0 test refs**, 6 call sites (`bot_executor.py:1561, 3252, 3606, 3857`; `ledger.py:1501`). One-way-mode compliance depends on this function; a regression here is a Binance-compliance break. (Retained v1 finding — the only one that survived verification.)
- `execute_exit_tp` (`bot_executor.py:2808`, 178 lines) — **0 test refs**. The TP exit path is exercised only indirectly via `handle_tp_completion` tests.
- `MetricsServer` (`engine/metrics.py:52`) — **0 test refs**.

**Parity/orphan paths:**
- `close_unattributed_position` (`parity_gates.py:1731`, 129 lines) — **0 test refs**. Closes exchange positions the ledger doesn't own — real-money path, untested directly.
- `diagnose_pair_orphans` (`parity_gates.py:1627`) — **0 test refs**.
- `mark_order_filled` (`ledger.py:42`, 141 lines, 2 direct DB writes) — **0 test refs**.

**Fill attribution:**
- `_attribute_anonymous_fill` (`ws_event_handlers.py:335`, 148 lines) — **0 test refs** (also 0 for the public name). Anonymous-fill attribution is core to proof-only reconciliation.

**Fault-injection (WS/network):**
- disconnect: **0 test files**; out-of-order events: **0**; chaos: **0** (grep across tests/ at HEAD).
- reconnect: 2 files; partial fills: 16 files (well covered); WS health-check restart logic: 3 tests in `test_ws_health_check.py`.
- Gap: no test simulates a full disconnect→reconnect cycle with an in-flight fill landing during the gap (the exact scenario `sync_stale_open_orders` and the reconciler's offline-fill reconstruction exist to heal).

**INV-31 guard:**
- `tests/test_writequeue_bypass_no_poison.py` + `tests/test_write_queue.py` cover WQ behavior under pytest, but there is **no test that fails when a new direct trades/bot_orders write bypasses WQ** — the guard would have to be a scan (T2.5/T2.6), not a unit test.

---

## 5. Roadmap (re-ordered, every item single-session)

v1's Phase 1 items 1 (structured logging + rotation) and 2 (health endpoint) were false-premise: logging/rotation/endpoint all exist. Their real残余 gaps (correlation IDs, operational gauges, alert delivery) replace them, and the live-broken heartbeat task takes top priority. Effort: S ≤ half-day, M ≤ one day, L = decomposed below into S/M sessions.

### PHASE 1 — Fix live operations (week 1)

- **T1.1 (S)** Repair or remove `CryptoBot_Heartbeat_Monitor` schtask. It fires every minute against nonexistent `scripts/heartbeat/cron_monitor.py` and has failed since 2026-07-23. Options: (a) implement the `--threshold 120` monitor for real (engine heartbeat age from engine.log / last_shutdown.ts) and commit it, or (b) delete the schtask. Verification: `schtasks /query` shows Last Result 0.
- **T1.2 (M)** Add operational gauges to `engine/metrics.py`: WriteQueue queue depth (expose from `write_queue.py`), parity-gate trips (REQUIRE_MANUAL_PROOF transition counter), WS reconnect counter. MetricsServer thread already runs from `run_engine.py:75`.
- **T1.3 (M)** Alert delivery + thresholds: one channel (Telegram bot via the existing Hermes gateway, or SMTP — zero alerting code exists today), thresholds on `account_drawdown_percent` (gauge exists) and the T1.2 gauges. Verification: simulated breach round-trips < 60 s.
- **T1.4 (S)** RULE #0 guard tests for `_resolve_position_side_param` (6 call sites, 0 tests): testnet/live param matrices, positionSide suppression, one-way-mode regression.
- **T1.5 (S)** Correlation IDs: bind cycle_id/bot_id log context (grep "correlation" in engine/ today hits only indicators.py TA usage). `logging` + rotation already exist — do NOT rebuild them.

### PHASE 2 — Close the measured test gaps (weeks 2-3)

- **T2.1 (S)** Tests: `execute_exit_tp` (0 refs, 178 lines).
- **T2.2 (S)** Tests: `close_unattributed_position` + `diagnose_pair_orphans` (0 refs each).
- **T2.3 (S)** Tests: `_attribute_anonymous_fill` (0 refs) + `mark_order_filled` (0 refs, has direct DB writes).
- **T2.4 (M)** WS fault-injection: disconnect→reconnect cycle with an in-flight fill during the gap; out-of-order event handling (both 0 test files today).
- **T2.5 (S per module)** INV-31 residual adjudication — for each of the 8 modules in §3.3: migrate the residual writes to WQ or document a CODEBASE_GUIDE exemption (expected exemptions: `create_order_with_receipt` INV-16 receipt; `migration_002` startup; recovery-path atomicity). Modules: bot_executor (18), ledger (8), reconciler (6), cycle_loop (4), parity_gates (3), recovery (3), GTR (1), exchange_interface (1). Verification: grep command in §0 returns only exempted sites.
- **T2.6 (S)** Static RULE #0 scan (pre-commit or session-start hook; no .pre-commit-config.yaml exists): fail on `positionSide` in any order-placement code path.

### PHASE 3 — Maintainability (weeks 3-6, L items pre-decomposed)

- **T3.1 (S ×5-6)** Split `reconciler.resolve_net_mismatch` (2,962 lines) — one session per mismatch class (sign-flip / qty-drift / orphan-physical / ghost-virtual / mixed).
- **T3.2 (S ×3-4)** Continue `bot_executor.maintain_orders` (2,229 lines) cluster extraction — WQ internals are already modularized; extract the remaining dispatch body per cluster.
- **T3.3 (S ×3)** Split `reconciler._reconstruct_offline_fills_internal` (2,006 lines) per fill-source class.
- **T3.4 (S ×2)** Split `runner.cycle_loop.run_cycle` (706 lines) and `reconciler._adopt_from_physical_positions_internal` (1,055).
- **T3.5 (S per batch)** Type hints for the remaining 168/561 unannotated engine functions (30%), public APIs first.
- **T3.6 (S)** Repo hygiene: `git rm` tracked junk — `engine/database.py.bak2`, `engine/database.py.tmp`, `engine/ledger.py.patch`, `tests/test_adr011_mechanism_b.py.backup2/.backup3/.bak3/.before_fixture_change`, 4× `crypto_bot.db.backup_*` (backups belong in `backups/` zip flow, not git), stray match_output*.txt / bnb_resp.txt / btc_resp.txt; fix `requirements.txt` header (v0.9.0 → v5.3.7) and uncomment pytest.
- **T3.7 (S)** Dead config: delete `config/default_config.json` (zero tracked references; contradicts settings.py) or wire it through settings with validation.

### PHASE 4 — Production readiness (weeks 6-8)

- **T4.1 (M)** Engine containerization: add `run_engine.py` entrypoint + compose service with healthcheck (MetricsServer port as the check). UI containerization already exists; engine does not.
- **T4.2 (M)** One-command rollback: orchestrate docs/BACKUP.md flow + tracked restore scripts + post-restore startup-barrier verification into a single script.
- **T4.3 (S ×3, was L)** Chaos harness, split: (a) kill-and-restart mid-cycle recovery test; (b) WS-drop fill-attribution test; (c) disk-full log behavior. (0 chaos tests today.)

### ONGOING

- 20% of each session to debt items; every roadmap item lands with its verification command in the card body (repo rule: proof-before-claiming).

---

## 6. Success metrics (all mechanically checkable)

- [ ] `pytest --collect-only -q tests/` ≥ 610, `pytest -q` 0 failures
- [ ] `schtasks /query` → `CryptoBot_Heartbeat_Monitor` Last Result 0 (or task removed)
- [ ] §0 WriteQueue grep + §3.3 residual table → 0 non-exempt direct trades/bot_orders writes
- [ ] Every §4 zero-refs symbol has ≥ 1 passing test
- [ ] Alert round-trip < 60 s on simulated drawdown/queue-depth breach
- [ ] Engine runs in compose with passing healthcheck; one-command rollback rehearsed on a copy

## 7. Immediate next steps (kanban-ready)

1. T1.1 heartbeat schtask fix (assignee: studio-builder-a) — live operational bug, top priority.
2. T1.4 `_resolve_position_side_param` RULE #0 tests (assignee: studio-builder-a).
3. T2.5 INV-31 residual adjudication, first module `bot_executor` (assignee: studio-builder-a).

---

*This v2 rework replaces the REJECTED v1 in full. All figures re-measured 2026-09-07 at HEAD `0531b2c` with commands per §0; the measurement scripts are read-only (git/schtasks/AST/pytest --collect-only) and touched no engine state. The v1 attachment reference (`cqa_audit_raw.txt`, mis-stated as 4225 chars — actual 4367 bytes) remains at repo root as the v1-era raw input.*
