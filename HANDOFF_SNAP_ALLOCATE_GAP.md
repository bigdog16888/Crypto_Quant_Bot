# HANDOFF ITEM #4 — SNAP-ALLOCATE Zero Attribution Guard Gap

**Status:** Documented for review — **NO LIVE CHANGES TONIGHT**  
**Priority:** High (live attribution path)  
**Found:** 2026-08-12 forensic session  
**Related to:** entry_confirmed loop fix (last night), manual-close attribution fix (last night)

---

## Exact Condition (Root Cause)

**File:** `engine/database.py`  
**Function:** `update_active_positions_snapshot()`  
**Lines:** 2995–3064 (trigger at line 3035)

```python
v_net = get_pair_virtual_net(symbol)        # sum of trades.open_qty across ALL active bots for symbol (signed)
ph_net = data['size'] if side == 'LONG' else -data['size']  # exchange physical net from fetch_positions()

if abs(v_net - ph_net) < 0.001:             # CENT-LEVEL TOLERANCE ONLY
    logger.info(f"💎 [SNAP-ALLOCATE] Ticker {symbol} Net matches ({v_net:.4f}). Splitting into {len(bot_shares)} bot shares.")
    for share in bot_shares:
        if share['qty'] > 0:
            conn.execute(
                "INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (share['id'], symbol, share['dir'], share['qty'], share['avg'], ts)
            )
            owned_count += 1
```

**Zero attribution guard:** The **only** gate is `abs(v_net - ph_net) < 0.001`. If the sum of `trades.open_qty` across **all active bots** for a symbol happens to equal the exchange net position (within 0.001), the position is **auto-split across every active bot** for that symbol proportional to `recompute_invested_from_orders()` output.

- No `forensic_adopt_allowed()` check (unlike `_attribute_anonymous_fill`)
- No single-candidate requirement (unlike `_attribute_anonymous_fill`)
- No fill-event trigger — runs on **startup barrier step 6** + **periodic position snapshot refresh**
- Writes directly to `active_positions` (no `bot_orders` audit row, no `wipe_proof_source`)

**Mechanism name:** "Proof-Based Multi-Bot Virtual Component Allocation" (ADR-006 comment, line 2995-3003)

---

## Evidence of Live Impact

### 2026-07-27 (first observed)
```
20:08:48  WebSocketHandler: Position Update: SOLUSDC SHORT -0.37
20:08:54  SNAP-ALLOCATE: "Ticker SOLUSDC Net matches (-0.3700). Splitting into 4 bot shares."
```
Repeats every ~11s (lines 446, 509, 572 in engine.log.4)

### 2026-08-10 (yesterday)
```
15:34:23  WebSocketHandler: Position Update: SOLUSDC SHORT -0.37
15:34:25  SNAP-ALLOCATE: "Ticker SOLUSDC Net matches (-0.3700). Splitting into 4 bot shares."
```
Repeats every ~14s (lines 29232, 29292, 29338 in engine.log)

### Current DB state (read-only query)
- `trades` for SOL bots: only bot 100001 shows `open_qty=0.82` (cycle 23, ACTIVE)
- `active_positions`: only bot 100001 has row (SHORT 0.82 @ 76.96)
- `bot_orders` for 100001: **no single fill of ~0.37 exists** — only 0.13 entry (id 1383) + 0.24 grid (id 1385) = 0.37 combined, both in cycle 23, both bot-placed (clientOrderId = `CQB_100001_*`)

**Conclusion:** The −0.37 SHORT position observed on exchange at 15:34:23 **predates** bot 100001's current cycle entries. It was likely a manual/hedge/external fill. The bot's ledger (`trades.open_qty`) happened to sum to −0.37 at snapshot time (possibly from prior cycle residue or drift), triggering SNAP-ALLOCATE to **fabricate a per-bot allocation** across all 4 SOL bots. The allocation is a **mathematical artifact of the sum-match**, not a real attribution.

---

## Proposed Fix

Add a **forensic attribution gate** to SNAP-ALLOCATE, mirroring the `_attribute_anonymous_fill` pattern:

```python
# BEFORE the split loop (after line 3035):
if len(bot_shares) > 1:
    # Multiple bots have non-zero invested for this symbol — ambiguous attribution
    if not forensic_adopt_allowed():
        logger.warning(
            f"🛑 [SNAP-ALLOCATE-BLOCKED] {symbol}: {len(bot_shares)} bots have invested qty, "
            f"net match ({v_net:.4f}) but forensic_adopt_allowed=False. "
            f"Refusing multi-bot auto-split. Position will remain unassigned in active_positions."
        )
        # Fall through to BRIDGE-MISS path (orphan assignment) or skip
        # Option A: assign to single owner if exactly one bot has position_side matching
        # Option B: leave as orphan (owner_id=0) for manual resolution
        continue  # skip multi-bot split
```

**Alternative (stricter):** Require **exactly one bot** with `share['qty'] > 0` matching the exchange side — same logic as `_attribute_anonymous_fill` lines 482-500.

**Config toggle:** Reuse existing `config.ALLOW_FORENSIC_ADOPT` (parity_gates.py:41-42) — already false by default in production.

---

## Test Plan (Unit + Integration)

### Unit Test: `test_snap_allocate_attribution_gate.py`

```python
def test_snap_allocate_blocks_multi_bot_when_forensic_disabled():
    """SNAP-ALLOCATE must NOT auto-split across multiple bots when ALLOW_FORENSIC_ADOPT=False."""
    # Setup: 2 active bots for SOLUSDC, both with invested qty > 0
    # Mock get_pair_virtual_net to return -0.37
    # Mock fetch_positions to return SHORT 0.37
    # ALLOW_FORENSIC_ADOPT = False
    # Call update_active_positions_snapshot()
    # Assert: active_positions INSERT called 0 times for these bots (or only orphan row)
    # Assert: warning log contains "SNAP-ALLOCATE-BLOCKED"

def test_snap_allocate_allows_single_bot_when_forensic_disabled():
    """Single bot with matching side should still be assigned."""
    # Setup: 1 active bot for SOLUSDC with invested qty > 0
    # Same mocks
    # Assert: active_positions INSERT called once for that bot

def test_snap_allocate_allows_multi_bot_when_forensic_enabled():
    """When ALLOW_FORENSIC_ADOPT=True, multi-bot split proceeds (current behavior)."""
    # Setup: 2+ bots, forensic enabled
    # Assert: active_positions INSERT called for each bot with share['qty'] > 0
```

### Integration Test (existing testnet)

1. Disable `ALLOW_FORENSIC_ADOPT` (default)
2. Create scenario: 2+ bots on same symbol, exchange has position, `trades.open_qty` sum matches
3. Trigger `update_active_positions_snapshot()` (startup or manual)
4. Verify: no `SNAP-ALLOCATE` split log; `BRIDGE-MISS` orphan warning instead
5. Enable `ALLOW_FORENSIC_ADOPT=True`
6. Repeat → split should proceed

### Regression Guard

Add assertion in `update_active_positions_snapshot()`:
```python
# After split loop
if owned_count > 0 and len([s for s in bot_shares if s['qty'] > 0]) > 1:
    assert getattr(config, 'ALLOW_FORENSIC_ADOPT', False), \
        "Multi-bot SNAP-ALLOCATE split occurred without forensic gate!"
```

---

## Why This Is Separate From Last Night's Fixes

| Fix | Trigger | Gate | Target Table | Status |
|-----|---------|------|--------------|--------|
| Entry-confirmed loop | Bot-placed entry order fill | `entry_confirmed` flag + order_id anchor | `trades` cache | ✅ Fixed |
| Manual-close attribution (`_attribute_anonymous_fill`) | Anonymous WS fill (non-CQB clientOrderId) | `forensic_adopt_allowed()` + single-candidate | `bot_orders` | ✅ Fixed |
| **SNAP-ALLOCATE (this item)** | **Position snapshot refresh (startup/periodic)** | **NONE (cent-level sum match only)** | **`active_positions`** | **❌ GAP** |

Three **independent auto-attribution paths** exist in the codebase. This is the third, unguarded one.

---

## Next Step (Review Gate)

This item is **documented for review**. Do not merge or deploy without:
1. Code review of proposed gate placement
2. Unit tests passing (CI)
3. Testnet validation with `ALLOW_FORENSIC_ADOPT=False` (default)
4. Explicit approval to enable `ALLOW_FORENSIC_ADOPT=True` if multi-bot split is ever desired

**Add to kanban board as:** `SNAP-ALLOCATE attribution gate` — assignee: `reviewer` / `crypto-bot-dev`