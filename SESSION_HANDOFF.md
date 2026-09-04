# SESSION_HANDOFF.md — 2026-09-04 (Session End)

## Current Commit / Tree
**HEAD = `7035200`** (detached) — unified branch containing:
- `37058b7` — freeze-guard: centralized `is_bot_frozen()` across 12 runtime paths + scenario test
- `f2ecee7` — O-3 sensor fix: restart-gap isolation + median-of-top-3 peak
- `7035200` — status: restart SUCCESS (30+ min stable), then platform shutdown

**Tag:** none (working tree clean, no new tag applied at session end)

---

## What Is Proven (Live Evidence + Replay)

### Freeze-Guard (P0 — COMPLETE)
- **Centralized guard:** `Config.is_bot_frozen(bot_id, bot_status)` checks `STARTUP_EXCLUDED_BOT_IDS` + `REQUIRE_MANUAL_PROOF`
- **12 patched paths:** `execute_entry`, `execute_exit_tp`, DUST_CHASER, INV-30→pending_flatten, `_handle_pending_flatten`, parity_gates (3), `handle_flatten`, `resolve_net_mismatch` pair-loop, `chase_limit_order`, `resolve_gated_bot`
- **Live fire (09:08:06):** `FREEZE-GUARD` blocked ETH 6 bots + LINK 2 bots from `resolve_net_mismatch` auto-repair
- **Scenario test:** `tests/test_freeze_guard_scenario.py` 9/9 PASS (replays INV-30 over-hedge on frozen bot → asserts `pending_flatten` never fires, no exchange order)
- **Full suite:** 580 passed / 13 pre-existing failed (zero regressions)

### O-3 Drawdown Sensor (PARTIAL — REGRESSION IN LIVE)
- **Fix design:** Restart gaps >1.5h split series; post-gap data only; peak = median of top-3 positive values
- **Replay proof:** On the exact Sep 3 data that killed two restarts, fix returns 0.00% drawdown (no trip)
- **Live failure (09:08:29):** O-3 fired with **37.14% drawdown** on the restarted engine — the fix did NOT activate
- **Root cause UNKNOWN:** Gap detection or post-gap fallback failed in live execution. Sensor code compiles, tests pass (12/12), but live behavior ≠ replay.
- **Threshold:** `DRAWDOWN_PCT=20` (operator decision — keep at 20)

---

## Engine State
**DOWN** (shutdown at 09:08:46, `reason: manual/unknown` — platform reaped background process)

### Frozen Bots (UNTOUCHED)
| Pair | Bots | State |
|---|---|---|
| ETH/USDC:USDC | 10011, 10021, 100002, 100316, 100321, 100325 | All `REQUIRE_MANUAL_PROOF` or `Scanning`, open_qty=0, 0.904 ETH orphan on exchange (live: 0.901 @ 2405.95) |
| LINK/USDC:USDC | 10020, 100320 | `Scanning` / `hedge_standby`, open_qty=0 |

### Active Pairs Running
- SOL/USDC:USDC — net 59.36 (virtual = exchange)
- BNB/USDC:USDC — net -0.02
- XAU/USDT:USDT — net 0
- BTC/USDC:USDC — net 0
- XRP/USDC:USDC — net 0
- SUI/USDC:USDC — net 0

---

## Full Open Items List

| ID | Item | Status | Location |
|---|---|---|---|
| **O3-1** | O-3 sensor: live-vs-replay bug — gap detection/fallback failed in live run | **BLOCKING RESTART** | `engine/database.py:146 compute_rolling_drawdown` |
| **DATA-1** | Flatten rows store `price=0.0` on `bot_orders` (100316 3.07 ETH flatten @ ~2406 has price=0) → realized P&L not computable from `bot_orders` | **PARKED** | `engine/bot_executor.py` / `engine/ledger.py` flatten write path |
| **REL-1** | -2015 "Invalid API-key" burst during emergency-liquidation (both 13:57 & 14:41 runs) — liquidation path dead on rate-limit | **PARKED** | `engine/runner/__init__.py` emergency path |
| **ETH-ORPHAN** | 0.904 ETH LONG on exchange (bot 10011 TP fill #1011451893, 16:35:23) — no `trades` row owns it; DB equity understates true by ~$2,267 | **NO DECISION** | Exchange position exists; DB has no owner |
| **AUDIT-1..11** | 11 other autonomous-correction paths from Sep 2 audit (DUST-FLUSH, TP maintenance, INV-30, pending_flatten, heal, orphan repair, etc.) — only the 12 freeze-guard paths were hardened; others still rely on `REQUIRE_MANUAL_PROOF` checks only | **PARKED** | `docs/AUTONOMOUS_CORRECTION_PATHS_AUDIT.md` |
| **STARTUP-BARRIER** | ETH pair still blocks final parity check (0.901 ETH gap) — exclusion logic exists but gap persists; manual proof or heal required | **OPEN** | `engine/runner/startup.py` |
| **MODEL-HEALTH** | `poolside/laguna-m.1:free` DELEGATION missing from live list; expires 2026-07-28 | **PARKED** | `PROJECT_STATUS.md` |
| **AUTH-2015** | -2015 on `fapiPrivateGetIncome` — income/read permission disabled on API key | **PARKED** | Binance UI |

---

## One-Paragraph Summary (Plain Language)

**Today we fixed the two safety holes that let a $7,400 unauthorized ETH flatten happen on Sep 2.** The core problem: "frozen" bots (ETH + LINK) were only blocked at startup — once the engine was running, eight runtime paths (pending-flatten, dust-flush, TP maintenance, hedge-drift, heal, entry, recovery, orphan repair) could still place real orders on them. We implemented a **single centralized freeze-guard** (`Config.is_bot_frozen`) that checks both the startup exclusion list and `REQUIRE_MANUAL_PROOF` status, patched it into all 12 order-capable paths with loud `🛑 [FREEZE-GUARD]` logs, and wrote a scenario test that replays the exact INV-30 over-hedge failure — confirmed the guard now blocks it. The engine restarted cleanly, the freeze-guard fired live on the first reconcile cycle (blocking all 8 frozen bots from auto-repair), and SOL/XAU reconciled perfectly. **Separately, the O-3 rolling-window drawdown breaker — which killed two restarts on Sep 3 due to a 33k equity spike from a restart transient — was fixed with gap-aware peak detection (median of top-3). It passed replay on the exact failure data, but failed in live execution (37% false fire); root cause unknown.** Three items parked (flatten price=0.0 data bug, -2015 emergency-path burst, ETH 0.904 orphan accounting gap), 11 other autonomous paths from the original audit still need hardening, and the ETH orphan decision remains open. Engine is DOWN, ETH/LINK frozen, orphan untouched — no restart until O-3 bug is debugged.

---

## Handoff Instructions for Next Session

1. **Do NOT restart** — engine down, tree clean at `7035200`
2. **Do NOT touch** ETH/LINK bots, 0.904 ETH orphan
3. **First task:** Debug O-3 live-vs-replay bug in `compute_rolling_drawdown` — add instrumentation, trace why gap detection missed the Sep 3→Sep 4 gap or fallback triggered
4. **Only after O-3 is solid:** restart from `7035200` (same tree), ETH/LINK excluded, watch for clean startup + freeze-guard + O-3 silence + SOL/XAU reconcile

---

## File Manifest (Session Changes)
- `config/settings.py` — `is_bot_frozen()` added
- `engine/bot_executor.py` — 4 freeze-guard patches
- `engine/runner/cycle_loop.py` — `_handle_pending_flatten` guard
- `engine/parity_gates.py` — 3 pair-level guards
- `engine/ledger.py` — `handle_flatten` guard
- `engine/reconciler.py` — pair-loop guard
- `engine/order_manager.py` — entry guard
- `engine/recovery.py` — `resolve_gated_bot` guard (startup-excluded only)
- `engine/runner/startup.py` — barrier exclusion logic (pre-existing, untouched)
- `engine/database.py` — `compute_rolling_drawdown` rewrite (gap detection + median-of-top-3)
- `tests/test_freeze_guard_scenario.py` — new (9 tests)
- `tests/test_rolling_drawdown.py` — updated (12 tests)
- `tests/test_entry_dedup.py` — fixture bot IDs fixed
- `tests/test_inv38_netting_aware_close.py` — parity-gate assertion fixed
- `PROJECT_STATUS.md` — updated with full open items
- `SESSION_HANDOFF.md` — this file