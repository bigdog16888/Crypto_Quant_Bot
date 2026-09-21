# COVER MEMO — Writer Consolidation & State Verification

**Date**: 2026-09-19 (morning update)
**Status**: All artifacts PLANNED/PROPOSED — nothing applied to production
**Next action**: Operator review → decision on Option 1 vs Option 2

---

## URGENCY FLAGS

### 🔴 HIGH: Hedge Drift on XAUUSDT (bot 10019 + 100319)
- **Post-dedup state**: Parent (10019) SHORT 1.014, Child (100319) LONG 1.681
- **Net drift**: **0.667 LONG** (child over-hedged by 0.667 contracts)
- **Financial exposure**: ~$2,900 unhedged directional risk
- **Note**: Previous cover memo incorrectly reported 2.695 (absolute sum). Correct calculation: 1.681 - 1.014 = +0.667 LONG.
- **Action needed**: A3 patch (under-hedge detection) or manual intervention
- **Recommendation**: #1 priority once writer consolidation is decided

### 🟡 MEDIUM: Pair-Format Duplicates (RESOLVED IN TEST)
- **Original state**: 2 bots had duplicate rows (bot 10019: XAUUSDT vs XAU/USDT:USDT; bot 100324: SOLUSDC vs SOL/USDC:USDC)
- **Bug identified**: Original dedup logic kept wrong row (stale pair format, incorrect size)
- **Fix applied**: Cross-check against `trades.open_qty` to pick correct row
- **Test result**: Migration successful on copy DB (9→7 rows, both duplicates resolved correctly)
- **Blocking status**: ✅ RESOLVED — corrected logic verified on `crypto_bot_migration_v4.db`
- **Recommendation**: Apply during writer consolidation migration phase

### 🟢 LOW: Stale cycle on bot 100324
- **Current state**: `cycle_phase=IDLE` but `open_qty=0.23` (should be 0)
- **Impact**: Phantom position in trades cache
- **Recommendation**: Investigate after writer consolidation

---

## MIGRATION VERIFICATION (Test Copy Only)

**Test DB**: `crypto_bot_migration_v4.db` (copy of live DB, NOT modified)

**Before migration** (9 rows):
```
bot_id=10007, pair=BNBUSDC, side=SHORT, size=0.04
bot_id=10008, pair=SOL/USDC:USDC, side=LONG, size=0.23
bot_id=10016, pair=BTCUSDC, side=LONG, size=0.002
bot_id=10018, pair=SUIUSDC, side=LONG, size=58.6
bot_id=10019, pair=XAU/USDT:USDT, side=SHORT, size=1.014  ← CORRECT (matches trades)
bot_id=10019, pair=XAUUSDT,        side=SHORT, size=0.201  ← DUPLICATE (stale)
bot_id=100319, pair=XAU/USDT:USDT, side=LONG,  size=1.681
bot_id=100324, pair=SOL/USDC:USDC, side=LONG,  size=0.23
bot_id=100324, pair=SOLUSDC,        side=LONG,  size=0.23   ← DUPLICATE
```

**After migration** (7 rows):
```
bot_id=10007, pair=BNBUSDC, side=SHORT, size=0.04
bot_id=10008, pair=SOL/USDC:USDC, side=LONG, size=0.23
bot_id=10016, pair=BTCUSDC, side=LONG, size=0.002
bot_id=10018, pair=SUIUSDC, side=LONG, size=58.6
bot_id=10019, pair=XAU/USDT:USDT, side=SHORT, size=1.014  ✓ KEPT (matches trades.open_qty=1.014)
bot_id=100319, pair=XAU/USDT:USDT, side=LONG,  size=1.681
bot_id=100324, pair=SOL/USDC:USDC, side=LONG,  size=0.23  ✓ KEPT (matches trades.open_qty=0.23)
```

**Dedup logic**: Keep row whose `size` matches `trades.open_qty` (within 0.001). Fallback: keep most recent `last_updated`.

**Schema changes**:
- Added `normalized_pair TEXT` column
- Created UNIQUE index `idx_active_positions_unique` on `(bot_id, normalized_pair, side)`

**Live DB**: UNCHANGED — no writes made.

---

## ARTIFACTS PRODUCED

| Artifact | Path | Status |
|----------|------|--------|
| Option 1 diff (single-writer) | `planned_writer_consolidation_option1_diff.md` | ✓ Complete |
| Option 2 diff (coordinated-writer) | `planned_writer_consolidation_option2_diff.md` | ✓ Complete |
| Risk analysis (Option 1 staleness) | `planned_writer_consolidation_risk_analysis.md` | ✓ Complete |
| Patch evaluation (4 patches) | `planned_patch_evaluation_writer_context.md` | ✓ Complete |
| Migration test (corrected logic) | `crypto_bot_migration_v4.db` | ✓ Verified (9→7 rows) |
| Cover memo | `COVER_MEMO_20260918.md` | ✓ Updated (this document) |

---

## RECOMMENDATION: OPTION 1 (Single-Writer)

**Why**:
1. **Simplicity**: One writer path eliminates race condition entirely
2. **Safety**: Cycle loop runs every ~10s; staleness acceptable for UI/reconciler
3. **Alignment**: `update_full_snapshot` already does atomic `trades` + `active_positions` update
4. **Lower risk**: Fewer code paths to test, easier rollback

**When Option 2 might be better**:
- If real-time freshness is critical (UI shows stale data for 10s)
- If startup/reconciler need immediate write access (not typical)

---

## PROPOSED SEQUENCE (If Approved)

### Phase 1: Standalone Patches (Low Risk)
1. Apply `cycle_id_filter_fix.patch` (ledger computation fix)
2. Apply `cycle_advance_guard.patch` (safety net for cycle advancement)

### Phase 2: Writer Consolidation Migration
3. Run migration script on PROD DB (with Rule-8 snapshot protocol):
   - Add `normalized_pair` column
   - Deduplicate existing rows (9→7 expected)
   - Create UNIQUE index
4. Apply Option 1 code changes:
   - Deprecate W2 (make no-op)
   - Change W4 to soft-clear (UPDATE size=0)
   - Add read-only getter `get_active_positions_snapshot()`
   - Update all call sites (13 total)

### Phase 3: Verification
5. Run full sweep:
   - Check for pair-format duplicates (should be 0)
   - Check for bot_id=0 orphans (should be 0)
   - Check parity: `active_positions` vs `exchange_fills`
6. Run test suite: `pytest tests/` on Python 3.10

### Phase 4: Post-Consolidation
7. Re-evaluate B3 patch (hedge drift fix)
   - If Option 1: Check if W1 cycle loop already handles hedge maintenance
   - If hybrid: B3 may still be needed
8. Monitor for 24h, then consider removing W2 code entirely

---

## ROLLBACK PLAN

If anything fails:
1. **Phase 1 failure**: Revert individual patch (`git checkout -- engine/database.py`)
2. **Phase 2 failure**: Restore DB from pre-migration snapshot (Rule-8 protocol)
3. **Phase 3 failure**: Disable new writer path via feature flag; restart engine with old code

**Rollback gates** (must pass before declaring success):
- `active_positions` row count ≤ pre-migration count (no new orphans)
- Zero `bot_id=0` rows for pairs with active bots
- `audit_pair_ledger_vs_exchange()` returns zero rows for all pairs
- Two-tier health: tier-1 drift = 0, tier-2 = only whitelisted orphans (BNB/SOL/SUI/XAU)

---

## QUESTIONS FOR OPERATOR

1. **Option 1 vs Option 2**: Do you prefer single-writer (simpler, 10s staleness) or coordinated-writer (complex, near-real-time)?
2. **Hedge drift priority**: Should we fix XAUUSDT drift (A3 patch) before or after writer consolidation?
3. **Migration timing**: Want to run migration tonight, or wait until morning?
4. **Testing**: Should we run full test suite (`pytest tests/`) before or after migration?

---

## FILES TO REVIEW

1. `COVER_MEMO_20260918.md` — This document (updated with corrected drift + dedup status)
2. `planned_writer_consolidation_option1_diff.md` — Full diff for single-writer approach
3. `planned_writer_consolidation_option2_diff.md` — Full diff for coordinated-writer approach
4. `planned_writer_consolidation_risk_analysis.md` — Staleness failure scenarios
5. `planned_patch_evaluation_writer_context.md` — Patch recommendations
6. `crypto_bot_migration_v4.db` — Test DB with migration applied (read-only, safe to inspect)

**No production changes have been made.** All artifacts are review-ready.
