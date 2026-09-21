# PATCH EVALUATION — Writer-Map Context Assessment

## CONTEXT
Writer-map analysis (2026-09-18) identified 3 active uncoordinated writers to `active_positions`:
- **W1**: `update_full_snapshot` in `cycle_loop.py` (cycle loop)
- **W2**: `update_active_positions_snapshot` called from `startup.py`, `reconciler.py`, `monitor.py`
- **W4**: `clear_active_position_for_bot` called from `database.py:2062`, `database.py:2416`

**Key finding**: W1 and W2 both do full-table DELETE+INSERT with different ownership logic → race condition → orphan positions (bot_id=0), pair-format duplicates, false drift alerts.

**Recommendation**: Before applying any patch, resolve writer consolidation (Option 1 or 2). Patches should be re-evaluated post-consolidation.

---

## PATCH 1: `cycle_id_filter_fix.patch`

**What it fixes**: `recompute_invested_from_orders` incorrectly applies `wipe_wall_ts` to current-cycle fills, causing cycle_floor to pull too much history and compute wrong position.

**Writer-map impact**: **STILL VALID** — This patch fixes a ledger computation bug that is INDEPENDENT of the writer race. The bug exists in `database.py:~4495` and affects all callers regardless of which writer populated `active_positions`.

**Recommendation**: **APPLY STANDALONE** — This is a pure logic fix, no schema changes, no writer interaction. Safe to apply before or after writer consolidation.

**Risk if deferred**: Bots with `wipe_wall_ts` set (post-TP resets) will continue to compute wrong `total_invested` and `avg_entry_price`.

---

## PATCH 2: `cycle_advance_guard.patch`

**What it fixes**: `_seal_trade_state_internal` in `ledger.py` advances `cycle_id` even when historical cycles have unclosed positions, creating phantom "missing cycle" alerts.

**Writer-map impact**: **PARTIALLY REDUNDANT** — The guard prevents bad cycle advancement, but the ROOT CAUSE (stale `active_positions` feeding false drift detection) is the writer race. If writer consolidation eliminates stale data, this patch becomes less necessary.

**However**: The guard also protects against genuine historical unclosed positions (e.g., hedge child gaps, manual interventions). This is a **safety net** independent of writer state.

**Recommendation**: **APPLY WITH CAVEAT** — Apply now as defense-in-depth, but plan to revisit after writer consolidation. If writer consolidation proves complete, this patch can be simplified (remove the "flag for manual review" logic and replace with auto-detect).

**Risk if deferred**: Cycle advancement continues to create phantom "missing cycle" states that require manual reconciliation.

---

## PATCH 3: `B3_idempotent_hedge_placement.patch`

**What it fixes**: Hedge child (e.g., bot 100319 for XAUUSDT) falls behind parent (bot 10019) in fill accumulation. B3 places idempotent aggregate catch-up orders to close the drift.

**Writer-map impact**: **DEPENDS ON OPTION 2** — B3 requires `WriteQueue` serialization to prevent race with W2/W4. Under Option 1 (single-writer), B3 is unnecessary because W1 runs every cycle and handles hedge maintenance atomically.

**Under Option 2** (coordinated-writer via WriteQueue): B3 fits naturally — aggregate catch-up goes through queue with MEDIUM priority, same as other W2 calls.

**Recommendation**: **DEFER** — Do not apply until writer consolidation is chosen. If Option 1 is chosen, B3 may be unnecessary (W1 handles hedge maintenance). If Option 2 is chosen, B3 can be adapted to use `_with_queue()` wrapper.

**Risk if deferred**: Hedge drift continues (XAUUSDT is the primary example). No safety risk — drift is detectable and can be manually resolved.

---

## PATCH 4: `active_positions_normalized_pair.patch`

**What it fixes**: Adds `normalized_pair` column + UNIQUE index to eliminate pair-format duplicates (e.g., `XAUUSDT` vs `XAU/USDT:USDT`).

**Writer-map impact**: **ESSENTIAL FOR BOTH OPTIONS** — This patch addresses the pair-format inconsistency that ALL THREE writers (W1, W2, W4) contribute to. Without it, duplicates persist regardless of writer consolidation.

**Migration requirement**: The patch assumes a one-time dedup script runs before enabling the UNIQUE constraint. Our test migration (see `planned_writer_consolidation_risk_analysis.md`) verified this works: 9 rows → 7 rows (2 duplicates removed).

**Recommendation**: **APPLY AS PART OF MIGRATION** — This patch should be applied DURING the migration phase of writer consolidation (Option 1 or 2). It's not standalone — it requires the `normalized_pair` column to exist before W1/W2/W4 can use it.

**Integration note**: The patch modifies `init_db()` to add the column, plus updates all writer functions to use `normalized_pair` in WHERE clauses. This aligns with both Option 1 and Option 2.

**Risk if deferred**: Pair-format duplicates persist, causing false "orphan" alerts (bot 10019 has 2 rows for XAUUSDT SHORT).

---

## SUMMARY TABLE

| Patch | Writer-Map Impact | Recommendation | Timing |
|-------|-------------------|----------------|--------|
| `cycle_id_filter_fix` | Independent | **APPLY STANDALONE** | Now |
| `cycle_advance_guard` | Partially redundant | **APPLY WITH CAVEAT** | Now, simplify later |
| `B3_idempotent_hedge` | Depends on Option 2 | **DEFER** | Post-consolidation |
| `normalized_pair` | Essential for both | **APPLY DURING MIGRATION** | With writer consolidation |

---

## PROPOSED SEQUENCE

1. **Apply `cycle_id_filter_fix`** (standalone, low risk)
2. **Apply `cycle_advance_guard`** (defense-in-depth)
3. **Run writer consolidation migration** (includes `normalized_pair` patch)
   - Phase 1: Add `normalized_pair` column + dedup (test DB verified)
   - Phase 2: Apply Option 1 or 2 code changes
   - Phase 3: Verify parity + rollback plan ready
4. **Re-evaluate B3** post-consolidation
   - If Option 1: Check if W1 cycle loop already handles hedge drift
   - If Option 2: Adapt B3 to use `WriteQueue`
