# Autonomous DB-Correction Code Paths — Corroboration Audit

**Date:** 2026-08-28  
**Context:** LINK cascade revealed two paths with single-read-trust problems (hedge-live-guard, filled integrity). Need full inventory before deciding what else needs hardening.

---

## Complete Inventory of Autonomous DB-Correction Paths

| # | Path | File/Function | Trigger | Reads Exchange? | Corroboration / Sanity Check | Single-Read Trust? | Risk |
|---|------|---------------|---------|-----------------|------------------------------|-------------------|------|
| **1** | **Hedge-Live-Guard (INV30)** | `bot_executor.py` ~line 2880 | Every cycle for hedge children | `fetch_positions()` | **NONE** — single read, no rate limit, no cooldown | ✅ **YES** (today's bug) | **CRITICAL** |
| **2** | **Filled Status Writes** | Multiple (`bot_executor.py`, `parity_gates.py`, `database.py`) | Various | Sometimes | **NONE** — can mark `filled` without exchange fill | ✅ **YES** (today's bug) | **CRITICAL** |
| **3** | **GTR Reconcile All** | `reconciler.py` `reconcile_all()` → `_reconcile_all_internal()` | Periodic (every cycle?) | `reconstruct_offline_fills()`, `_align_memory_to_ledger()`, `_cleanup_phantom_entries()`, ghost detection | **Multi-step**: offline fills require CID match; memory alignment is DB-internal; ghost detection uses `fetch_positions()` | ⚠️ **PARTIAL** — ghost detection uses single `fetch_positions()` | **HIGH** |
| **4** | **Reconstruct Offline Fills** | `reconciler.py` `reconstruct_offline_fills()` | Startup + periodic (15m cooldown) | `fetch_order_history()` / `fetch_my_trades()` | **CID-based verification** — only credits fills matching `clientOrderId` prefix | ❌ NO — requires CID proof | **LOW** |
| **5** | **Align Memory to Ledger** | `reconciler.py` `_align_memory_to_ledger()` | After offline fills | None (DB only) | **DB-only** — compares `trades` vs `bot_orders` | ❌ NO — no exchange read | **LOW** |
| **6** | **Phantom Entry Cleanup** | `reconciler.py` `_cleanup_phantom_entries()` | Every reconcile cycle | None (DB only) | **DB-only** — finds `total_invested>0` but `entry_confirmed=0` and `avg_entry=0` | ❌ NO — no exchange read | **LOW** |
| **7** | **Ghost Detection (INV26)** | `oneway_netting.py` `detect_bot_ghost()` | GTR reconcile cycle | `fetch_positions()` via `get_exchange_signed_net()` | **Single `fetch_positions()`** — compares virtual net to physical | ✅ **YES** — same pattern as hedge-live-guard | **HIGH** |
| **8** | **Wipe Bot Ghost** | `oneway_netting.py` `wipe_bot_ghost()` | After ghost detection | `fetch_positions()` (re-verifies) | **Re-verifies** before wipe: calls `get_exchange_signed_net()` again | ⚠️ **PARTIAL** — re-verifies but still single read each time | **MEDIUM** |
| **9** | **One-Way Repair** | `oneway_netting.py` `reconcile_oneway_pair_open_qty()` | Startup | `fetch_positions()` + `fetch_ticker()` | **Sign-aware classification** + `MAX_OWAY_REPAIR_QTY` guard + requires `current_price > 0` | ⚠️ **PARTIAL** — single position read, but multiple guards | **MEDIUM** |
| **10** | **Phantom Purge** | `parity_gates.py` `purge_phantom_ledger_when_exchange_flat()` | Startup repair + manual | `fetch_positions()` + `fetch_open_orders()` | **Requires `physical ≈ 0`** (exchange flat) + cancels open orders first | ⚠️ **PARTIAL** — single flat check, but conservative (only acts when exchange=0) | **MEDIUM** |
| **11** | **Orphan Exchange Repair** | `parity_gates.py` `repair_exchange_orphan_when_ledger_flat()` | Startup repair | `fetch_positions()` + market close | **Requires `virtual ≈ 0` AND `physical > 0`** + `_orphan_repair_allowed()` guard (human approval flags, cooldown) | ⚠️ **PARTIAL** — multiple guards but single position read | **MEDIUM** |
| **12** | **Reconcile Pair to Exchange** | `parity_gates.py` `reconcile_pair_to_exchange()` | Startup repair | `fetch_positions()` | **Routes to purge/orphan/trim** — each has own guards | ⚠️ **PARTIAL** — delegates to guarded paths | **MEDIUM** |
| **13** | **Startup Repair Mismatched** | `parity_gates.py` `startup_repair_mismatched_pairs()` | Startup | `fetch_positions()` via `audit_pair_ledger_vs_exchange` | **Requires NO gated bots on pair** + delegates to `reconcile_pair_to_exchange` | ⚠️ **PARTIAL** — gating check is strong | **LOW-MEDIUM** |
| **14** | **Safe Wipe Bot** | `database.py` `safe_wipe_bot()` | Manual close, SL, phantom purge, orphan repair | `fetch_positions()` (Guard 2.0) | **Guard 2.0**: live-verifies exchange flat **before AND after** close | ❌ NO — double verification | **LOW** |
| **15** | **Global Wipe Detection** | `parity_gates.py` `detect_and_repair_global_wipe()` | Startup | `fetch_positions()` for all pairs | **Requires ALL pairs flat on exchange** + `ENABLE_GLOBAL_WIPE_DETECTION` flag | ⚠️ **PARTIAL** — comprehensive but single snapshot | **LOW** |
| **16** | **Sync Stale Open Orders** | `bot_executor.py` `sync_stale_open_orders()` | Every cycle | `fetch_order()` per order | **Per-order verification** — checks each order status individually | ❌ NO — verifies each order | **LOW** |
| **17** | **Pre-Commit Resolve** | `database.py` `reconcile_with_db()` | Order placement | `fetch_positions()` + `fetch_open_orders()` | **Multi-source** — compares DB, exchange positions, open orders | ⚠️ **PARTIAL** — single snapshot but multiple sources | **MEDIUM** |

---

## Classification Summary

### 🔴 CRITICAL — No Corroboration, Single-Read Trust (Today's Bugs)
| Path | Fix Required |
|------|--------------|
| **1. Hedge-Live-Guard** | Multi-read corroboration + startup cooldown (see `HEDGE_LIVE_GUARD_HARDENING.md`) |
| **2. Filled Status Writes** | Enforced `exchange_fill_id` requirement (see `FILLED_INTEGRITY_FIX.md`) |

### 🟠 HIGH — Single `fetch_positions()` Read for Corrective Action
| Path | Current Guard | Gap |
|------|---------------|-----|
| **3. GTR Ghost Detection** (`detect_bot_ghost`) | Compares virtual vs physical | Single `fetch_positions()` read; no rate-of-change bound; no startup cooldown |
| **7. Ghost Detection (INV26)** | Same as above | Same code path |

### 🟡 MEDIUM — Single Read But Conservative Guards
| Path | Guards | Residual Risk |
|------|--------|---------------|
| **8. Wipe Bot Ghost** | Re-verifies before wipe | Still single read each verification |
| **9. One-Way Repair** | Sign check + `MAX_OWAY_REPAIR_QTY` + price check | Single position read |
| **10. Phantom Purge** | Only acts when exchange=0 | Conservative (safe direction) |
| **11. Orphan Repair** | `_orphan_repair_allowed` + human flags | Single position read |
| **12. Reconcile Pair** | Delegates to guarded paths | Depends on delegate |
| **14. Pre-Commit Resolve** | Multi-source snapshot | Single snapshot |

### 🟢 LOW — No Exchange Read / Strong Gating / Double Verification
| Path | Why Safe |
|------|----------|
| **4. Reconstruct Offline Fills** | CID-proof required — no trust, only verify |
| **5. Align Memory to Ledger** | DB-only, no exchange read |
| **6. Phantom Entry Cleanup** | DB-only, no exchange read |
| **13. Startup Repair** | Skips if ANY gated bot on pair |
| **14. Safe Wipe Bot** | Guard 2.0: verifies flat BEFORE AND AFTER |
| **15. Global Wipe Detection** | Requires ALL pairs flat |
| **16. Sync Stale Orders** | Per-order verification |

---

## Recommended Hardening Priority

### Phase 1 (Immediate — Today's Bugs)
1. **Hedge-Live-Guard** → Multi-read + cooldown (design doc done)
2. **Filled Integrity** → `exchange_fill_id` required (design doc done)

### Phase 2 (Before Any Un-freeze)
3. **GTR Ghost Detection** (`detect_bot_ghost` / `wipe_bot_ghost`) — same single-read pattern as hedge-live-guard
   - Apply same multi-read corroboration
   - Add startup cooldown
   - Add rate-of-change bound

### Phase 3 (Before Production)
4. **One-Way Repair** — add multi-read corroboration for `fetch_positions()`
5. **Pre-Commit Resolve** — add rate-of-change bound on position reads
6. **Phantom Purge / Orphan Repair** — add second verification read

### Phase 4 (Ongoing)
7. **Audit all `fetch_positions()` callers** for single-read-trust pattern
8. **Centralize position fetching** with built-in corroboration (wrapper that enforces multi-read)

---

## Test Fixtures Needed (Per Path)

| Path | Fixture | Source |
|------|---------|--------|
| Hedge-Live-Guard | `tests/fixtures/hedge_live_guard_link_cascade.json` | Today's LINK cascade |
| Filled Integrity | `tests/fixtures/filled_integrity_link_cascade.json` | Today's LINK cascade |
| Ghost Detection | `tests/fixtures/ghost_detection_noise.json` | Synthetic: cycling position reads |
| One-Way Repair | `tests/fixtures/one_way_repair_sign.json` | Existing `test_oneway_repair_sign.py` |
| Pre-Commit Resolve | `tests/fixtures/pre_commit_resolve_jitter.json` | Synthetic: position jitter at order placement |

---

## Centralized Position Fetch Wrapper (Future)

```python
# engine/exchange_interface.py — new function
async def fetch_positions_corroborated(
    self,
    symbol: str,
    min_reads: int = 3,
    window_sec: int = 10,
    max_change_pct: float = 0.5,
    startup_cooldown_sec: int = 300,
) -> Optional[float]:
    """
    Fetch position with built-in corroboration.
    Returns median of consistent reads, or None if:
    - Startup cooldown active
    - Reads inconsistent (> max_change_pct)
    - Rate of change exceeded
    """
    if startup_cooldown_active(startup_cooldown_sec):
        return None
    
    readings = []
    for _ in range(min_reads):
        pos = await self.fetch_positions(symbol)
        readings.append(pos)
        await asyncio.sleep(window_sec / min_reads)
    
    if max(readings) - min(readings) > median(readings) * max_change_pct:
        return None  # Inconsistent
    
    return median(readings)
```

**All autonomous correction paths should use this wrapper instead of raw `fetch_positions()`.**

---

## Decision Required

Review this audit. Confirm:
1. Phase 1 design docs approved (hedge-live-guard, filled integrity)
2. Phase 2 (ghost detection) added to design scope
3. Whether to implement centralized wrapper first or harden each path individually

No implementation until design review complete.