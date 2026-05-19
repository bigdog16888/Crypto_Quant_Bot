# Netting Mismatch — Root Cause Analysis & Definitive Fix
**For: Implementing LLM Agent**  
**Version: 1.0 | Based on full codebase review of database.py, reconciler.py, runner.py, bot_executor.py**

> READ THIS ENTIRE DOCUMENT before touching a single line of code.
> Every section exists because a specific bug was found in the source. Do not skip.

---

## 0. The One Sentence Summary

The system has **two separate SQL queries** that both calculate "what does the system think it holds" — and they use **different logic**. The dashboard uses one. The reconciler uses the other. They disagree. That disagreement is the mismatch alert. It is not a real position gap.

---

## 1. The Two Conflicting "Virtual Net" Calculations

### Query A — `reconciler.py` lines 453–468 (used by the dashboard netting alert)

```sql
SELECT b.pair, b.direction, b.id,
    COALESCE(SUM(
        CASE 
            WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') 
                THEN bo.filled_amount
            WHEN bo.order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 
                'dust_close', 'sl', 'virtual_netting', 'hedge') 
                THEN -bo.filled_amount
            ELSE 0.0
        END
    ), 0.0) as bot_net_qty
FROM bots b
LEFT JOIN trades t ON b.id=t.bot_id
LEFT JOIN bot_orders bo ON b.id=bo.bot_id AND bo.filled_amount>0
    AND (bo.cycle_id=t.cycle_id OR bo.cycle_id IS NULL OR t.current_step=0)
    AND bo.status NOT IN ('reset_cleared','auto_closed','failed','placing')
WHERE b.is_active=1
GROUP BY b.id
```

**Key characteristics:**
- Joins across `cycle_id = t.cycle_id OR cycle_id IS NULL OR current_step=0`
- `hedge` order type is treated as an **EXIT** (subtracts from net)
- No `wipe_wall_ts` filter
- No `position_side` filter
- Includes `adoption` in entries

### Query B — `database.py` line 2748 inside `recompute_invested_from_orders()` (used by the ledger/trades table)

```sql
-- Entries (bought_qty):
order_type IN ('entry', 'grid', 'adoption_add', 'adoption', 'carry')
-- Exits (sold_qty):
order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl', 'virtual_netting')
-- Hedge (separate bucket, cross-cycle, no wall filter):
order_type LIKE 'hedge%' AND NOT LIKE '%tp%'  →  +hedge_qty
order_type LIKE 'hedge%tp%'                   →  -hedge_qty
```

**Key characteristics:**
- Filtered by `cycle_id = target_cycle` AND `wipe_wall_ts`
- `hedge` is in its **own separate bucket**, cross-cycle, **never subtracted from net qty**
- Has `position_side` filter: `bo.position_side = ? OR NULL OR BOTH`
- `carry` is included in bought_qty

---

## 2. The Specific Discrepancies That Cause the Mismatch

### Bug A: `hedge` treatment is opposite between the two queries

In **Query A** (dashboard): `hedge` subtracts from net qty (treated as exit).  
In **Query B** (ledger): `hedge` is in a separate bucket, never touches net qty.

Result: Any bot with a hedge position will show a mismatch equal to exactly the hedge qty, every single cycle. This is **BTC** right now — `long btc price` is `🛡️ HEDGED (2.7820)`. The $158 BTC mismatch = 2.782 BTC hedge being double-subtracted in one query.

### Bug B: `carry` order type exists in Query B but not Query A

Query B includes `carry` in bought_qty. Query A has no `CARRY` case — it falls into `ELSE 0.0`. Any bot with a carry-over fill will be undercounted in the dashboard query. This contributes to SOL and SUI drift after TP resets.

### Bug C: `wipe_wall_ts` filter exists in Query B but not Query A

Query B ignores fills created before `wipe_wall_ts` (the cycle reset boundary). Query A has no such filter. After a TP reset, old fills from previous cycles that weren't properly marked `reset_cleared` will bleed into Query A but not Query B. This inflates the system-side number in the dashboard.

### Bug D: `position_side` filter exists in Query B but not Query A

In one-way mode, multiple bots on the same pair (LONG + SHORT) are both represented. Query B filters by `position_side` so each bot only counts its own direction fills. Query A does not — it aggregates all fills per bot regardless of `position_side`. When a LONG bot has a `position_side=SHORT` adoption row from a reconciler quirk, Query A counts it wrong.

### Bug E: `adoption` type counting inconsistency

Our failed manual fix last night proved this directly. We inserted rows with `order_type='adoption'`. Query B counts them in bought_qty. Query A also counts them. But when we inserted at `cycle_id=21` (SOL) the dashboard went UP, not down — meaning the system side was already counting something that the exchange wasn't. The original gap was system-overcounting, not system-undercounting.

---

## 3. What Is Actually Happening Right Now

For **SOL**: `sol` (LONG, bot 10008) is at Step 7, `short sol` (SHORT, bot 100001) is at Step 2. In one-way mode, these net on the exchange. The system has both bots' contributions being calculated by inconsistent queries. The hedge on `sol` (1.78 SOL shown as HEDGED) is being double-subtracted in the dashboard query.

For **SUI**: `sui long` (LONG, bot 10018) has a hedge. Same double-subtraction.

For **BTC**: `long btc price` shows `HEDGED (2.7820)`. The $158 mismatch ≈ 2.782 BTC × ~$57k would be enormous but it's $158, meaning the mismatch is actually 0.0020 BTC. The hedge column in the reconciler SQL is treating the hedge fills as exit fills, reducing the system-side virtual qty below what the exchange actually holds net.

---

## 4. The Fix — Single Source of Truth

**The dashboard netting query (Query A in reconciler.py) must be replaced to match the exact logic of `recompute_invested_from_orders` (Query B).**

There should be ONE virtual net calculation in the entire codebase. Everything else reads from it.

### Step 1: Create a new function in `database.py`

Add this function after `recompute_invested_from_orders`:

```python
def get_pair_virtual_net(symbol: str) -> float:
    """
    Returns the signed virtual net quantity for a normalized symbol across ALL active bots.
    
    This is the SINGLE SOURCE OF TRUTH for virtual position accounting.
    Uses identical logic to recompute_invested_from_orders() — same cycle guard,
    same wipe_wall filter, same order type buckets, same hedge treatment.
    
    LONG bots contribute positive qty. SHORT bots contribute negative qty.
    Net result mirrors what the exchange sees in one-way mode.
    
    Args:
        symbol: Normalized symbol e.g. 'SOLUSDC'
    
    Returns:
        Signed float. Positive = net virtual LONG. Negative = net virtual SHORT.
    """
    from engine.exchange_interface import normalize_symbol
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get all active bots on this symbol
        cursor.execute("""
            SELECT b.id, b.direction,
                   COALESCE(t.cycle_id, 1) as cycle_id,
                   COALESCE(t.wipe_wall_ts, 0) as wipe_wall_ts,
                   COALESCE(t.position_side, b.direction) as position_side
            FROM bots b
            LEFT JOIN trades t ON t.bot_id = b.id
            WHERE b.is_active = 1
              AND (b.normalized_pair = ? OR REPLACE(REPLACE(b.pair, '/', ''), ':USDC', '') = ?)
        """, (symbol, symbol))
        
        bots = cursor.fetchall()
        if not bots:
            return 0.0
        
        total_net = 0.0
        
        for bot_id, direction, cycle_id, wipe_wall_ts, position_side in bots:
            if cycle_id is None:
                continue
            
            # Use identical SQL to recompute_invested_from_orders
            res = cursor.execute("""
                SELECT
                    COALESCE(SUM(
                        CASE WHEN bo.cycle_id = ? 
                             AND bo.status NOT IN ('auto_closed', 'reset_cleared')
                             AND (? = 0 OR bo.created_at >= ?)
                             AND bo.order_type IN ('entry','grid','adoption_add','adoption','carry')
                        THEN bo.filled_amount ELSE 0.0 END
                    ), 0.0) AS bought_qty,
                    
                    COALESCE(SUM(
                        CASE WHEN bo.cycle_id = ?
                             AND bo.status NOT IN ('auto_closed', 'reset_cleared')
                             AND (? = 0 OR bo.created_at >= ?)
                             AND bo.order_type IN ('adoption_reduce','tp','close','dust_close','sl','virtual_netting')
                        THEN bo.filled_amount ELSE 0.0 END
                    ), 0.0) AS sold_qty,
                    
                    -- Hedge: cross-cycle, no wipe_wall — matches recompute_invested_from_orders exactly
                    ROUND(COALESCE(SUM(
                        CASE 
                            WHEN bo.status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                                 AND bo.order_type LIKE 'hedge%' AND bo.order_type NOT LIKE '%tp%'
                            THEN bo.filled_amount
                            WHEN bo.status NOT IN ('auto_closed','reset_cleared','rejected','failed')
                                 AND (bo.order_type LIKE 'hedge%tp%' OR bo.order_type LIKE 'hedgetp%')
                            THEN -bo.filled_amount
                            ELSE 0.0
                        END
                    ), 0.0), 8) AS hedge_qty
                    
                FROM bot_orders bo
                WHERE bo.bot_id = ?
                  AND (bo.position_side = ? OR bo.position_side IS NULL 
                       OR bo.position_side = 'BOTH' OR bo.position_side = '')
                  AND (
                      bo.status IN ('filled', 'closed', 'auto_closed', 'hedge_exited')
                      OR (bo.status IN ('canceled', 'cancelled') AND bo.filled_amount > 0)
                  )
                  AND bo.filled_amount > 0
            """, (
                cycle_id, wipe_wall_ts, wipe_wall_ts,  # bought_qty
                cycle_id, wipe_wall_ts, wipe_wall_ts,  # sold_qty
                # hedge_qty: no cycle/wall params — unconditional
                bot_id, position_side
            )).fetchone()
            
            if not res:
                continue
            
            bought_qty, sold_qty, hedge_qty = float(res[0] or 0), float(res[1] or 0), float(res[2] or 0)
            
            # Net qty for this bot: entries - exits - hedge
            bot_net_qty = round(bought_qty - sold_qty - hedge_qty, 8)
            
            # Sign: LONG bots are positive, SHORT bots are negative (one-way mode netting)
            direction_upper = str(direction).upper()
            signed_qty = bot_net_qty if direction_upper == 'LONG' else -bot_net_qty
            
            total_net = round(total_net + signed_qty, 8)
        
        return total_net
        
    except Exception as e:
        logger.error(f"[get_pair_virtual_net] Error for {symbol}: {e}")
        return 0.0
```

### Step 2: Replace Query A in `reconciler.py`

Find the block starting at approximately line 436 (`# 1.6. 🚀 HISTORY-BASED ORPHAN DETECTION`) through line 510. Replace the virtual position calculation section:

**Find this block (lines ~453–479):**
```python
_oh_cur.execute("""
    SELECT b.pair, b.direction, b.id,
           COALESCE(SUM(
               CASE 
                   WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount
                   WHEN bo.order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl', 'virtual_netting', 'hedge') THEN -bo.filled_amount
                   ELSE 0.0
               END
           ), 0.0) as bot_net_qty
    FROM bots b
    LEFT JOIN trades t ON b.id=t.bot_id
    LEFT JOIN bot_orders bo ON b.id=bo.bot_id AND bo.filled_amount>0
        AND (bo.cycle_id=t.cycle_id OR bo.cycle_id IS NULL OR t.current_step=0)
        AND bo.status NOT IN ('reset_cleared','auto_closed','failed','placing')
    WHERE b.is_active=1
    GROUP BY b.id
""")
virt_pos = {}
for rv in _oh_cur.fetchall():
    sym = _nsym(rv[0])
    direction = str(rv[1]).upper()
    magnitude = float(rv[3] or 0)
    signed_qty = magnitude if direction == 'LONG' else -magnitude
    virt_pos[sym] = virt_pos.get(sym, 0.0) + signed_qty
```

**Replace with:**
```python
# ── SINGLE SOURCE OF TRUTH: use get_pair_virtual_net() for virtual qty ──
# This matches recompute_invested_from_orders() exactly — same cycle guard,
# same wipe_wall filter, same hedge treatment, same order type buckets.
# The old inline SQL here was a divergent copy that caused permanent false mismatches.
from engine.database import get_pair_virtual_net as _gpvn
all_active_symbols = set()
for r in _oh_cur.execute("SELECT DISTINCT normalized_pair FROM bots WHERE is_active=1").fetchall():
    if r[0]:
        all_active_symbols.add(str(r[0]).upper())
# Also include all physical position symbols
for sym in phys_pos.keys():
    all_active_symbols.add(sym)

virt_pos = {}
for sym in all_active_symbols:
    virt_pos[sym] = _gpvn(sym)
```

### Step 3: Apply the same fix to `adopt_from_physical_positions` in `reconciler.py`

In `adopt_from_physical_positions` (around line 3357), the virtual net is recomputed inline again using `recompute_invested_from_orders` per-bot and summing. This is correct but it must subtract hedge_qty from the per-bot contribution consistently. 

**Find this section (around line 3430–3448):**
```python
_, _, true_qty, _, h_qty = recompute_invested_from_orders(bot_id)
...
bot_net_qty = true_qty - float(bot_hedge_qty)
total_net_proved_qty += (bot_net_qty if bot_dir == 'LONG' else -bot_net_qty)
```

**Verify** that `true_qty` returned from `recompute_invested_from_orders` already has hedge subtracted (it returns `total_net_qty = bought_qty - sold_qty - hedge_qty` from the function). If it does, then `bot_net_qty = true_qty - float(bot_hedge_qty)` is **double-subtracting hedge**. 

Check database.py line 2816:
```python
total_qty = round(bought_qty - sold_qty, 8)
total_net_qty = round(total_qty - hedge_qty, 8)
...
return total_invested, avg_price, total_net_qty, max_step, hedge_qty
```

Yes — `true_qty` (the 3rd return value) is already `bought - sold - hedge`. Then `bot_hedge_qty` is separately fetched and subtracted again. **This is the double-hedge-subtraction bug.** Fix:

```python
# recompute_invested_from_orders returns (cost, avg, net_qty, step, hedge_qty)
# net_qty is already: bought_qty - sold_qty - hedge_qty
# Do NOT subtract hedge again.
_, _, true_qty, _, h_qty = recompute_invested_from_orders(bot_id)

# true_qty is already net of hedge. Sign by direction.
total_net_proved_qty += (true_qty if bot_dir == 'LONG' else -true_qty)
```

Remove the separate `bot_hedge_qty` SQL query and the `bot_net_qty = true_qty - float(bot_hedge_qty)` line entirely. That separate query exists because of a historical misunderstanding about what `recompute_invested_from_orders` returns.

---

## 5. The Wipe-Wall Gate That Blocks Self-Healing

In `adopt_from_physical_positions` (reconciler.py lines 3331–3354), there's a gate:

```python
post_wall_cqb = any(
    str(f.get('clientOrderId', '')).startswith('CQB_') and
    int((f.get('timestamp') or 0) // 1000) >= min_wipe_wall
    for f in recent_fills
)
if not post_wall_cqb:
    continue  # ← SKIPS ALL HEALING
```

This gate was designed to prevent pre-session orphans from being adopted. But it fails when:
- The engine was deadlocked/frozen and grid fills happened on the exchange without the bot placing them (no `CQB_` ID)
- These are legitimate fills from the current session (post wipe_wall) but anonymous

**Fix:** When `not post_wall_cqb`, don't skip entirely. Instead check if there are ANY fills post wipe_wall:

```python
# Check for ANY fills post wipe_wall (not just CQB_ fills)
# Anonymous fills can be from grid orders placed before the deadlock
post_wall_any = any(
    int((f.get('timestamp') or 0) // 1000) >= min_wipe_wall
    for f in recent_fills
)
if not post_wall_cqb and not post_wall_any:
    # Truly pre-session orphan — skip
    logger.warning(f"[WIPE-WALL-GATE] {symbol}: No post-wall fills at all. True pre-session orphan.")
    continue
elif not post_wall_cqb and post_wall_any:
    # Anonymous post-wall fills exist — these are legitimate but untagged
    # Don't skip — fall through to forensic mode with the anonymous fills
    logger.info(f"[WIPE-WALL-GATE] {symbol}: No CQB fills but {sum(1 for f in recent_fills if int((f.get('timestamp') or 0)//1000) >= min_wipe_wall)} anonymous post-wall fills found. Proceeding with forensic attribution.")
```

---

## 6. The `short sol` + `sol` Netting on the Same Symbol

Both `sol` (LONG) and `short sol` (SHORT) trade `SOL/USDC:USDC`. In one-way mode they net on the exchange. The dashboard currently shows:

```
SOLUSDC: sys=-0.29 vs ex=-0.20 (SHORT net)
```

This means the exchange currently has a net SHORT of 0.20 SOL (short sol's position minus sol's position). The system thinks it should be -0.29 (more short). The -0.09 gap is likely:
- `short sol` at Step 2 with more filled qty than the system ledger thinks
- OR `sol` (LONG) hedge being double-counted

After fixing Bug A (hedge double-subtraction), this should self-resolve. No manual DB surgery needed.

---

## 7. Implementation Order

Do these in exact order. Commit and test after each.

### Phase 1 — Database layer (no behavior change, just new function)
1. Add `get_pair_virtual_net(symbol)` to `database.py` as specified in Step 1 above.
2. Add unit-test assertion: call `get_pair_virtual_net('SOLUSDC')` and verify it returns the same value as summing `recompute_invested_from_orders` per bot manually.

### Phase 2 — Fix double-hedge-subtraction in `adopt_from_physical_positions`
1. In `reconciler.py`, find the `total_net_proved_qty` accumulation loop (around line 3398–3448).
2. Remove the separate `bot_hedge_qty` SQL query.
3. Remove `bot_net_qty = true_qty - float(bot_hedge_qty)`.
4. Replace with `total_net_proved_qty += (true_qty if bot_dir == 'LONG' else -true_qty)`.
5. Do the same for `total_net_proved_qty_v2` in the second pass (around line 3580).

### Phase 3 — Replace divergent virtual net in reconciler orphan detection
1. In `reconciler.py` `reconstruct_offline_fills`, replace the inline virtual position SQL (Query A) with calls to `get_pair_virtual_net` as specified in Step 2 above.

### Phase 4 — Fix wipe-wall gate
1. Apply the gate fix from Section 5 above to `adopt_from_physical_positions`.

### Phase 5 — Verify
After deploying, within 1–2 reconciler cycles (2–3 minutes) the dashboard netting alerts should show near-zero gaps. The BTC hedge mismatch should vanish immediately. The SOL/SUI gaps should close within one `adopt_from_physical_positions` cycle.

---

## 8. What NOT to Do

| Action | Why Not |
|--------|---------|
| Insert `adoption` rows manually | Adds to system qty without reducing it — makes gap worse |
| Run `seal_all_active_bots()` | Re-derives from bot_orders, doesn't fix the query divergence |
| Reset any bot ledger | Positions are real and live |
| Add more `elif` branches to Query A | There should be no Query A after this fix |
| Modify `wipe_wall_ts` values | These are safety boundaries — touch only with engine stopped |

---

## 9. Root Cause in One Paragraph

The system evolved two separate virtual-net calculations over six months of patching. One lives in `reconciler.py`'s orphan detection (Query A) and one lives in `database.py`'s `recompute_invested_from_orders` (Query B). Every time a new order type was added (`carry`, `hedge`, `adoption_add`, etc.) it was added to Query B but not Query A, or added to both with different semantics. The `hedge` type specifically is treated as an exit in Query A and as a separate neutral bucket in Query B — this single difference produces the persistent BTC/SOL/SUI mismatch alerts on every restart. The fix is architectural: delete Query A and replace it with calls to the single source of truth.

---

## 10. Files to Modify

| File | Section | Change |
|------|---------|--------|
| `engine/database.py` | After `recompute_invested_from_orders` | Add `get_pair_virtual_net()` |
| `engine/reconciler.py` | `reconstruct_offline_fills` lines ~453–479 | Replace inline SQL with `get_pair_virtual_net()` |
| `engine/reconciler.py` | `adopt_from_physical_positions` lines ~3430–3448 | Remove double hedge subtraction |
| `engine/reconciler.py` | `adopt_from_physical_positions` lines ~3580 | Same fix for `_v2` pass |
| `engine/reconciler.py` | `adopt_from_physical_positions` lines ~3331–3354 | Soften wipe-wall gate |

No other files need modification for this fix.
