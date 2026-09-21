# Prioritized Plan — Track B (Fill-Crediting Reliability) & Remaining Track A
**Generated:** 2026-09-18 offline period — for operator review

---

## Executive Summary

The investigation revealed that the core fill-crediting pipeline has **three interacting failure modes** that compound:
1. **Cycle boundary confusion** — `recompute_invested_from_orders` uses `wipe_wall_ts` and `cycle_floor` that exclude valid current-cycle fills and pull in historical noise
2. **Cycle advancement past unclosed positions** — Mechanism B advances `cycle_id` without checking if prior cycles have net-open positions
3. **Hedge child fill accounting gap** — Parent fills trigger hedge signals, but hedge child fills don't fully propagate back to parent's hedge accounting

These three issues explain all observed gaps: SOL/10008, SUI/10018, BNB/10007, BTC/10016, XAU duplicate AP, SUI hedge child 100323.

---

## PROPOSED: Track A — Remaining Items (Priority Order)

### A4: Fix `recompute_invested_from_orders` Cycle Boundary Logic (HIGHEST PRIORITY)
**Files:** `engine/database.py` (lines 4473-4680)
**Draft Patch:** `patches/cycle_id_filter_fix.patch`
**Why first:** This is the root computation engine. Every position query, seal, reconciliation, and hedge decision depends on it. If it returns wrong numbers, everything downstream is wrong.

**Fix Scope:**
- When `cycle_id=None` (current cycle), ignore `wipe_wall_ts` for the target cycle — only apply to historical cycles
- When explicit `cycle_id` passed, compute for that cycle ONLY without historical FIFO pollution
- Fix `cycle_floor` auto-detection to not pull in cycles that have been properly closed
- Add `effective_wall_ts` logic: 0 for current cycle, actual wall_ts for historical

**Risk:** Medium — core function, but changes are isolated to boundary logic. Must verify with full regression suite.

---

### A5: Guard Cycle Advance Against Historical Unclosed Positions
**Files:** `engine/ledger.py` (lines 1135-1203)
**Draft Patch:** `patches/cycle_advance_guard.patch`
**Why second:** Mechanism B advances cycles based only on current-cycle fills. It doesn't see that cycles 19-20 (SOL/10008) or cycle 25+ (SUI/10018) have unclosed positions. The guard prevents advancing past "orphaned" cycles.

**Fix Scope:**
- Before incrementing `cycle_id`, scan all cycles `< current_cycle_id` for net-open positions
- If found, block advance, write `[MANUAL-REVIEW]` to `bots.notes` (A2 flagging pattern)
- Log `[SEAL-CYCLE-BLOCK]` with details

**Risk:** Low — additive guard, only blocks advance, doesn't change existing behavior when clean.

---

### A6: Fix Hedge Child Fill Propagation (WS fill crediting)
**Files:** `engine/ws_event_handlers.py`, `engine/bot_executor.py` (`_signal_hedge_child_entry`, `_maintain_hedge_child`)
**Why third:** The XAUUSDT duplicate AP row (0.201 SHORT on parent 10019) and SUI hedge child 100323 orphan fill indicate hedge child fills aren't properly credited to parent's hedge accounting.

**Root Cause Hypothesis:** 
- Hedge child fills come via WS → `handle_order_update` → `credit_fill` → child's `bot_orders` + child's `trades`
- But parent's hedge tracking (`parent_hedgeable_qty` in `_maintain_hedge_child`) reads from parent's `bot_orders`, not child's
- The `active_positions` table gets synced from `exchange_fills` by reconciler — pair format mismatch (`XAUUSDT` vs `XAU/USDT:USDT`) creates duplicate AP rows

**Fix Scope:**
1. In `ws_event_handlers.py`: When crediting a hedge child fill, also update parent's hedge accounting
2. In reconciler: Normalize pair formats before upserting `active_positions` to prevent duplicates
3. Add hedge child fill → parent accounting linkage in `credit_fill`

**Risk:** Medium — touches WS path and parent/child coupling.

---

### A7: Reconcile Pair Format Normalization
**Files:** `engine/reconciler.py`, `engine/database.py` (active_positions upsert)
**Why fourth:** The duplicate AP row for bot 10019 (`XAUUSDT` and `XAU/USDT:USDT` both present) is a direct result of exchange returning one format, WS returning another, reconciler not normalizing.

**Fix Scope:**
- Add `normalize_pair(pair: str) -> str` utility
- Apply at all AP upsert points: reconciler, WS fill credit, manual adoption

**Risk:** Low — single utility, applied consistently.

---

## PROPOSED: Track B — Fill-Crediting Reliability (Priority Order)

### B1: Idempotent Fill Crediting with Deduplication (HIGHEST PRIORITY)
**Files:** `engine/ws_event_handlers.py`, `engine/database.py` (`credit_fill`, `fill_claims`)
**Why:** The current WS path can double-credit fills if the same fill event is delivered multiple times (reconnect, replay). `fill_claims` table exists but isn't fully enforced on WS path.

**Fix Scope:**
- Every WS fill event generates a deterministic claim key: `bot_id:order_id:fill_timestamp:filled_amount`
- Check `fill_claims` BEFORE crediting; skip if exists
- Insert claim atomically with credit
- Add `claim_key` column to `bot_orders` for traceability

**Risk:** Medium — WS path is hot path. Must not add latency.

---

### B2: Exchange-Side Fill Verification Before Credit
**Files:** `engine/ws_event_handlers.py`, `engine/parity_gates.py`
**Why:** WS fill events can be spurious (test orders, phantom fills). Before crediting, verify the fill exists on exchange via `fetch_order` or `fetch_my_trades`.

**Fix Scope:**
- For each WS fill event, do async exchange verification (with cache)
- Only credit if exchange confirms
- Cache verification results for 30s to avoid rate limits

**Risk:** Medium — adds exchange call to WS path. Use async/background verification.

---

### B3: Idempotent Aggregate Hedge Order Placement (PREREQUISITE FOR A3 FIX)
**Files:** `engine/bot_executor.py` (`_maintain_hedge_child`)
**Draft Patch:** `patches/B3_idempotent_hedge_placement.patch`
**Why:** A3 aggregate detection exists but is detection-only. B3 makes it corrective by placing ONE aggregate catch-up order with deterministic CID for idempotency.

**Fix Scope:**
- When aggregate drift > 2× tolerance, place single aggregate order
- CID format: `CQB_{bot_id}_AGGREGATE_{cycle_id}_CATCHUP` (deterministic)
- Exchange-side idempotency check via `fetch_order(cid)`
- Skip per-step catch-up if aggregate placed

**Risk:** Medium — new order placement path. Must respect position limits and live guard.

---

### B4: Live Hedge Guard Enhancement
**Files:** `engine/bot_executor.py` (`_signal_hedge_child_entry`, `_maintain_hedge_child`)
**Why:** The live guard exists for `_signal_hedge_child_entry` (lines 5530-5604) but the per-step catch-up in `_maintain_hedge_child` (lines 3287-3340) has a separate, less complete guard.

**Fix Scope:**
- Unify live guard logic into single function
- Apply to both initial hedge entry AND catch-up orders
- Use same `get_exchange_signed_net` fetch for both

**Risk:** Low — refactoring existing logic.

---

### B5: Reconciliation-Driven Fill Correction (Automated)
**Files:** `engine/reconciler.py` (`reconstruct_offline_fills`, `reconcile_all`)
**Why:** The dry_run wrapper and RECONCILER_LIVE_APPROVED gate exist. When enabled, the reconciler should automatically correct fill discrepancies found during reconciliation.

**Fix Scope:**
- In `reconcile_all` (live mode): When drift detected, call corrected fill crediting
- Use same idempotent claim mechanism as B1
- Write `bot_orders` reconciliation markers (already exists: `drift_note`, `reconciliation_marker`)

**Risk:** Medium — automated correction needs careful gating.

---

## PROPOSED: Dependency Graph

```
A4 (recompute fix) ──┐
                     ├──→ A5 (cycle advance guard) ──→ A6 (hedge child propagation)
                     │
A7 (pair normalize) ──┤
                      ├──→ B1 (idempotent fill credit)
B2 (exchange verify) ─┤
                      ├──→ B3 (aggregate hedge placement) ──→ A3 becomes CORRECTIVE
B4 (unified live guard) 
                      ├──→ B5 (auto reconciliation correction)
```

---

## PROPOSED: Recommended Execution Order

1. **A4** — Fix the computation engine (validated by hash-diff on dry_run)
2. **A7** — Normalize pair formats (low risk, enables clean reconciliation)
3. **A5** — Guard cycle advance (prevents new orphans)
4. **B1** — Idempotent fill crediting (fixes WS double-credit root cause)
5. **B2** — Exchange verification (hardens B1)
6. **A6** — Hedge child propagation (uses B1/B2 infrastructure)
7. **B4** — Unify live guard (cleanup)
8. **B3** — Aggregate hedge placement (unlocks A3 corrective)
9. **A3** — Flip from detection-only to corrective (trivial once B3 exists)
10. **B5** — Automated reconciliation (capstone)

---

## PROPOSED: Validation Gates (Each Step)

Before committing any step:
1. **Hash-diff test** on `reconstruct_offline_fills(dry_run=True)` — 7 tables must match
2. **Full regression suite** — 12 tests must pass
3. **Manual spot-check** on affected bots (10008, 10018, 10007, 10016, 10019, 100323, 100324)
4. **Diff review** — each commit independently reviewable

---

## PROPOSED: Effort Estimates

| Item | Est. Effort | Risk | Blockers |
|------|-------------|------|----------|
| A4 | 1 day | Medium | None |
| A7 | 0.5 day | Low | None |
| A5 | 0.5 day | Low | A4 |
| B1 | 1 day | Medium | A7 |
| B2 | 1 day | Medium | B1 |
| A6 | 1 day | Medium | B1, B2 |
| B4 | 0.5 day | Low | B1, A6 |
| B3 | 1 day | Medium | B4 |
| A3→corrective | 0.5 day | Low | B3 |
| B5 | 1 day | Medium | B1, B5 |

**Total: ~8 days of focused work, sequentially gated.**

---

## PLANNED: Immediate Decisions Needed from Operator

1. **Bots 10008/10018:** Flatten on exchange? Adopt with proof? Current state: STOPPED, zeroed trades, AP shows positions.
2. **Bot 100323:** The 125.1 FLATTEN fill is orphaned — adopt to hedge child trades? Parent 100000 context?
3. **RECONCILER_LIVE_APPROVED=1:** When to enable? (Requires B1-B3 complete for safety)
4. **A3→A4 scope:** Confirm A3 should become corrective once B3 exists, or remain detection-only indefinitely?

---

## Notes

- All draft patches are in `patches/` directory, **NOT applied**
- No commits, no tags, no pushes during offline period
- All investigation was read-only; **NO `bots.notes` flags were written** — the "PLANNED" items in the findings document describe intended actions, not executed ones
- Full sweep table in `INVESTIGATION_FINDINGS_20260918_OFFLINE.md` section 5