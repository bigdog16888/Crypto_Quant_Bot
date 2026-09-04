# O-3 False Fire Root Cause — 2026-09-04 (37.14% drawdown, blocking restart)

**Status:** ROOT CAUSE CONFIRMED (raw log + DB + exchange evidence). Fix design proposed below — NOT implemented (operator gate).

**Head at investigation:** `5a6094f` (handoff commit on top of `7035200`).

---

## 1. What fired, exactly

```
2026-09-04 09:08:29,828 - BotRunner - CRITICAL - O-3 ROLLING-WINDOW DRAWDOWN TRIGGERED! 37.14% drop over 24.0h window
```

The f2ecee7 fix (gap-isolation + median-of-top-3 peak) **worked as designed** — it is NOT the bug. Replay of the exact live series through `compute_rolling_drawdown` reproduces the fire bit-for-bit:

```
LIVE REPLAY: drawdown=37.14% peak=15088.81  (log said 37.14%)
gap detected: 18.44 h (> 1.5h threshold -> post-gap isolation active)
```

The gap logic correctly isolated post-restart data. The peak logic correctly picked the median (15088.81), not the spike. **The bug is the input, not the algorithm:** `current_equity = 9485.24` at 09:08:29 was a false reading caused by a data race between the equity computation and the engine's own SOL flatten.

## 2. Mechanism (dated log lines, 09:08:22 → 09:08:29)

Equity = `Cash + Cost + uPnL` (engine/runner/__init__.py:288, `check_circuit_breaker`).

| Time | Cash | Cost | uPnL | Equity | Event |
|---|---|---|---|---|---|
| 09:08:22 | 8694.34 | 5951.43 | 443.03 | **15088.81** | Normal cycle reading |
| 09:08:29.219 | — | — | — | — | INV-30: parent 100001 hedgeable=0 → child 100324 flagged over-hedged 59.30 SOL |
| 09:08:29.466 | — | — | — | — | PENDING-FLATTEN market close placed (`CQB_100324_FLATTEN_1788484109`) |
| 29.466→.511 | — | — | — | — | Flatten fills (58.16 SOL @ ~103.37, WS confirms close) |
| 09:08:29.749 | — | — | — | — | SNAP-ALLOCATE re-links residual LONG SOLUSDC to 100324 (tertiary) |
| 29.736→29.813 | 9034.20 | **13.72** | 437.32 | **9485.24** | SAFE-WIPE + cycle advance wipes 100324's Cost → equity computed mid-transition |

The delta: Cost dropped **5951.43 → 13.72** between two cycles 7 seconds apart. Cash rose only +$339.86 (8694.34→9034.20) — the ~$6,010 of SOL cost-basis "saved" by the wipe appeared in cash only later (balance REST fetch raced the flatten fills). The SOL position was **not sold at a loss** — the ledger transition itself (wipe → cycle advance) temporarily removed the position's accounting while cash hadn't yet caught up. O-3 sampled exactly that transient window.

**This is the same class as the Sep 3 failure the fix was designed for (33k up-spike) but on the opposite side of the ratio: a DOWN-transient in `current_equity`, which no snapshot-side hardening can prevent** because `current_equity` enters `compute_rolling_drawdown` as a trusted scalar, not as a snapshot series point that the top-3 median can filter.

## 3. Secondary findings (all raw-verified)

1. **O-3's emergency path fired and caused real exchange mutations.** The breaker tripped `handle_emergency_liquidation()` → BNB bot 10007's TP + GRID orders emergency-cancelled at 09:08:33 (DB rows now `cancelled`). Position itself (0.02 BNB SHORT, $13.72) survived. No position was liquidated (zero "Emergency Market Close" lines all-time).
2. **Shutdown sequence correction:** the handoff's "platform-reaped background process" is WRONG. The engine exited through its OWN graceful path: O-3 wrote `engine.emergency` → main loop detected it (run_engine.py:142) → `handle_emergency_liquidation` → `runner.running = False` → shutdown sequence (WriteQueue flush, shutdown-seal, `last_shutdown.ts` written 09:08:46, SocketLock released "manual/unknown" — the default `reason` string, not evidence of manual action). The -2015 storm in 09:08:30–09:08:44 was the emergency path's `fetch_positions` calls (REL-1, pre-existing: ccxt-native calls on demo-fapi hit -2015; the wrapper's raw path works — which is why the emergency path is dead but the engine's own wrapper fetches succeeded).
3. **SOL is NOT "running" as the handoff states.** Live exchange check (GET only): SOL flat (only ETH 0.901 LONG + BNB −0.02 SHORT remain). DB `active_positions` still holds a stale 100324 SOL 58.16 row (last_updated 09:08:29, refresh raced the shutdown). The Sep 4 flatten sold the entire SOL book position including the netted parent/child book: exchange went from +59.36 to 0, while trades 100001/100324 open_qty=0 — so SOL pair now has a Pattern-E-class physical-vs-virtual mismatch (exchange 0 vs DB... see §4) and will hit the startup barrier's parity check at next restart.
4. The test suite (12/12) never covers the current-equity-side transient because all tests feed `compute_rolling_drawdown` clean series; none test a mid-transition equity read.
4b. **The INV-30→pending_flatten→wipe path executed on SOL with ZERO freeze-guard question raised** — SOL bots are not frozen, so the guard correctly did not block. But note the trigger: parent 100001 "hedgeable=0" came from the Sep 3 plausibility-block state (DB flat / exchange +59.36). The Sep 4 flatten resolved that mismatch by flattening the child, netting the whole pair. Whether that was the intended resolution path for Pattern-E parent-flat-child-physical is a **judgment call for the operator** — it is the same "engine auto-resolves a parity mismatch by trading" class that froze ETH/LINK.

4c. **Steady-state check (no-race simulation):** even without the 7-second race, the next snapshot reads ~40.04% vs the post-gap baseline, because the post-gap baseline itself (15094/15088) contains the SOL Cost (5951) that the legitimate flatten removes. **So a restart now, even with perfect timing, re-fires O-3 at 20% threshold** — the 37.14% was not purely transient; a large component is real accounting transition (cost removal) measured against a pre-flatten baseline.

## 4. Current state snapshot (2026-09-04, post-investigation)

| Pair | Exchange (live GET) | DB trades.open_qty | active_positions | Notes |
|---|---|---|---|---|
| ETH | 0.901 LONG (entry 2405.95, uPnL +84.85) | 0 (frozen) | 100325 LONG 0.901 | Known orphan; untouched |
| BNB | −0.02 SHORT (entry 686.1, uPnL −0.72) | 0.02 (10007) | 10007 SHORT 0.02 | **No TP protection** — orders cancelled by the emergency path |
| SOL | 0 (flattened 09:08:29) | 0 | **stale row 100324 58.16** | Exchange-confirmed close (fill #402877873 FILLED) |

---

## 5. Fix design (proposed — awaiting operator review)

The root cause has two layers; both must be addressed:

### Layer 1 — The equity read raced the flatten (the immediate trigger)

**A. Don't sample equity mid-transition.** `check_circuit_breaker` is called at cycle_loop.py:747 AFTER bot execution within the same cycle. The flatten wiped Cost during bot execution; equity was computed immediately after, before cash updated. Two complementary sub-fixes:

- **A1 (recommended, minimal):** Exclude `Cost` from O-3's equity. O-3 measures ACCOUNT drawdown — cash is the exchange ground truth; Cost is a DB-derived position accounting value prone to wipe/reset transients. `current_equity = total_stablecoin + unrealized_pnl` (positions marked to market, not cost-based). This kills the whole class: wipes, resets, cycle advances can no longer move the O-3 input.
  - Rationale from the raw data: with A1, the 09:08:29 read = 9034.20 + 437.32 = **9471.52** … still below peak. So A1 alone does NOT fully prevent the fire (the cash hadn't caught up yet at the moment of sampling) → A1 must be paired with B.
- **A2 (deeper, optional later):** After any flatten/wipe event, defer the equity snapshot by one cycle (flag set by the flatten handler, cleared next cycle) so the transition settles. More moving parts; A1+B makes it unnecessary for this failure mode.

**B. Median-of-top-3 for `current_equity` — symmetric to the peak fix.** The f2ecee7 fix hardened the peak; the current side got nothing. Apply the same median-of-last-3-snapshots to the current reading before comparing. The transient read 9485.24 would be filtered because 15094/15088 are the neighbors in the window. Implementation: in `_check_rolling_drawdown`, replace `current_equity` with the median of the last 3 recorded snapshots (need ≥2 agreeing reads).
  - Note: `record_equity_snapshot(current_equity, ts=now)` writes the transient INTO the series first — B must take the median of snapshots INCLUDING the just-written one, or the bad read is already persisted and future comparisons are poisoned. Median-of-last-3 at comparison time handles both.

**C. Rule: O-3 never fires on a drawdown that appeared in a single sample interval.** With B in place, a legitimate gradual 37% decline crossing 20% shows in multiple consecutive snapshots; a transient shows in exactly one. Enforce: require the drawdown condition to hold on N=2 consecutive checks before triggering. Consecutive-check confirmation is standard for breakers (avoids one-shot transients); the full emergency path runs once, correctly.

### Layer 2 — The post-flatten baseline is guaranteed to re-fire (the restart blocker)

Even with perfect timing, the steady state after the SOL flatten reads ~40% vs the post-gap baseline (§3.4c) because the baseline captured pre-flatten Cost. **Any restart now re-fires O-3 immediately.** Options:

- **O3-RESTART-1 (recommended):** At engine start, if a post-gap series exists, rebase the peak baseline to the FIRST snapshot of the new run (or use start-of-run equity as initial peak). The rolling window then measures drawdown within THIS run only — which is what "rolling" means for a breaker whose equity definition includes Cost. This matches the gap-isolation design already in f2ecee7 (post-gap data only): the peak search space and the current reading must come from the same run's data.
- **O3-RESTART-2:** Treat flatten-triggered cost removal as a non-event for the peak: when a flatten/wipe removes Cost ≥ X% of equity, reset the baseline. Complex, path-coupled; not recommended.
- **O3-RESTART-3:** Operator manually clears `equity_snapshots` (or rows above the gap) before restart. This is a DB write to crypto_bot.db — operator decision, not mine to make.

### What I would NOT do

- Raise the threshold (hides the problem; John explicitly kept 20).
- Disable O-3 on flatten events (a real crash could still occur during a flatten).
- Keep Cost in equity for O-3 (the entire Sep 4 incident — both the race AND the steady-state re-fire — comes from Cost being in the O-3 input).

## 6. Fix acceptance criteria (for when a design is approved)

1. Replay of the exact 09:08:29 live series + transient current read → NO fire.
2. Replay of a genuine gradual −25% decline (multiple snapshots) → fires at 20% crossing.
3. Restart scenario: post-gap baseline rebased to run start → no spurious fire on first cycles after SOL cost removal (e.g., SOL Cost 5951 → 0 while cash rises correspondingly).
4. `tests/test_rolling_drawdown.py` extended: (a) transient-current-read case, (b) genuine-decline case, (c) rebase case.
4b. Scenario test: "current read races a flatten wipe" — Cost collapse mid-cycle → median-of-3 + consecutive-confirm → no trigger, no emergency file, no order mutation.
5. Full suite green; no regressions in the 13 pre-existing failures.
6. Restart from a single commit hash with everything above.

## 7. Self-review

1. **Unverified claims:** none in the root cause itself — every mechanism claim has a dated log line, DB row, or live exchange read backing it. The one inference I did NOT verify: that the cash fetch raced the flatten (timing of the REST balance fetch vs fill) — inferred from Cash 8694→9034 (+$339) instead of +$6,010. Highly likely (fetch started before fills), but the exact fetch timing isn't logged. The steady-state simulation (§3.4c) covers this gap: even if cash had caught up, the fire still occurs (40.04%).
2. **Internal contradictions:** the handoff said "SOL net 59.36 running" — corrected: SOL was flattened to 0 by the engine at 09:08:29; handoff also said "shutdown = platform-reaped" — corrected: graceful engine-initiated shutdown via the O-3 emergency path. Both corrections backed by raw evidence above.
2b. **The handoff's own instruction #3 asked for exactly this debugging.**
3. **Silent scope narrowing:** I traced O-3's fire through the entire loop (entry → INV-30 → flatten → wipe → equity read → trigger → emergency → shutdown). The one branch not deep-dived: why INV-30 flagged the child over-hedged when the parent had been plausibility-gated flat since Sep 3 (trigger chain: parent hedgeable=0 because DB-flat since Sep 3 DNA-WIPE) — traced to its cause, but the policy question (whether flattening the child is the correct Pattern-E resolution) is operator territory (§3.4b).
4. **Mechanism honesty:** traced end-to-end with raw command output — snapshot table dump, dated Circuit Check lines, WS fill lines, replay reproducing 37.14% bit-for-bit, live exchange GET.
5. **Test coverage gaps:** full suite NOT run this session (engine down, investigation-only). The 12/12 O-3 tests pass but don't cover current-side transients — named in §3.4.
6. **Reversibility:** investigation was read-only (RO DB connections, GET-only exchange). No code, no DB writes, no restart. Fix proposal awaits approval.
