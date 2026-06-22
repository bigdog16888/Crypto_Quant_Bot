# ADR-002: Hedge Child Bot Architecture

**Status:** Approved  
**Version:** 1.0  
**Target Release:** v3.6.0  
**Replaces:** Current `order_type='hedge'` / `hedge_qty` implementation

---

## 1. Context — Why the Current Implementation Is Wrong

The current hedge system tracks a SHORT position *inside* a LONG bot using:
- `order_type='hedge'` and `order_type='hedge_tp'` rows in `bot_orders`
- `trades.hedge_qty` accumulator
- `cycle_phase='HEDGED'` / `'HEDGE_EXIT_PENDING'` states
- `get_bot_hedge_qty()`, `basket_open_qty_from_recompute()` utility functions
- `h_qty` fifth return value threaded through `recompute_invested_from_orders()`

This design violates the fundamental invariant of the ledger system: **every physical exchange position must have exactly one owning bot with matching direction**. A LONG bot cannot own a SHORT position. The consequences observed in production:

1. `update_active_positions_snapshot()` assigns `bot_id=0` (orphan) to any SHORT position created by a LONG bot's hedge orders — because the lookup `WHERE direction='SHORT'` finds no match.
2. The reconciler's `adopt_from_physical_positions()` sees the orphan SHORT and attempts forensic adoption, triggering false `REQUIRE_MANUAL_PROOF` states.
3. `recompute_invested_from_orders()` returns a 5-tuple `(cost, avg, qty, step, h_qty)`. Every caller must handle `h_qty` separately. As of this writing, `h_qty` is threaded through 14 call sites across 4 files.
4. `get_pair_virtual_net()` must subtract `hedge_qty` from LONG bots' signed contributions to avoid double-counting the exchange net — a per-bot adjustment that breaks if any hedge row is miscounted.
5. The canonical subselect `_BOT_ORDERS_CANONICAL_SUBSELECT` was patched in v3.5.7 specifically because `cancelled` hedge rows were losing to `auto_closed` zero-fill duplicates. The hedge accounting complexity directly caused that ranking failure.
6. `heal_zombie_bots()` had to be patched to check `hedge_qty <= 0.0001` to avoid wiping bots in HEDGED phase.
7. Bot 10017 (xrp long) accumulated 4,129 XRP of hedge fills across 59 cycles, producing a 44.7 XRP net SHORT permanently orphaned at `bot_id=0`.

**The 9 accommodation points for hedge in the current codebase:**

| Location | Accommodation |
|---|---|
| `database.py: recompute_invested_from_orders` | `h_qty` fifth return value, hedge-specific SQL CASE blocks |
| `database.py: get_bot_hedge_qty()` | Standalone function only needed because hedge lives in wrong bot |
| `database.py: basket_open_qty_from_recompute()` | Separates basket qty from hedge qty in recompute result |
| `database.py: get_pair_virtual_net()` | Subtracts hedge_qty per bot to compute correct pair net |
| `database.py: heal_zombie_bots()` | Guards Scenarios 1 & 3 with `hedge_qty <= 0.0001` |
| `ledger.py: seal_trade_state()` | Passes `h_qty` to trades, writes `hedge_qty` column |
| `ledger.py: handle_tp_completion()` | Skips hedge/hedge_tp orders when scanning for open orders |
| `reconciler.py: adopt_from_physical_positions()` | Special-cases `hedge_qty` in net proof calculation (7 sites) |
| `parity_gates.py: get_bot_signed_contribution()` | Subtracts `get_bot_hedge_qty()` from LONG bot's signed net |

All 9 are eliminated by this ADR.

---

## 2. Decision — Paired Bot Model

A hedge position is a **child bot**: a fully independent bot entity with its own row in `bots`, `trades`, and `bot_orders`, linked to a parent bot via a foreign key.

### Core Invariant
> A hedge child bot is a SHORT bot (when parent is LONG). It owns its SHORT position legitimately. Every physical SHORT on the exchange created by hedge orders is assigned to the hedge child bot. No special cases needed anywhere in the ledger, reconciler, or snapshot system.

---

## 3. Schema Changes

### 3.1 `bots` table — new columns

```sql
ALTER TABLE bots ADD COLUMN bot_type TEXT DEFAULT 'standard';
-- Values: 'standard' | 'hedge_child'
-- 'hedge_child' bots do not scan for entry signals independently.
-- They only act when signalled by their parent bot.

ALTER TABLE bots ADD COLUMN parent_bot_id INTEGER DEFAULT NULL;
-- Set on hedge_child bots. FK → bots.id of the parent.
-- NULL on all standard bots.

ALTER TABLE bots ADD COLUMN hedge_child_bot_id INTEGER DEFAULT NULL;
-- Set on parent bots that have a hedge child configured.
-- NULL if hedge is not enabled for this bot.

ALTER TABLE bots ADD COLUMN hedge_trigger_step INTEGER DEFAULT NULL;
-- Step number at which hedge activation begins.
-- e.g. 8 means: when parent fills step 8, hedge child places first entry.
-- NULL means hedge is disabled for this bot.
```

### 3.2 `trades` table — deprecate `hedge_qty`

`trades.hedge_qty` is kept as a column (no destructive migration) but:
- Is zeroed for all bots in the migration script
- Is never written after v3.6.0
- Is never read after v3.6.0
- Will be dropped in a future version (v4.0.0)

### 3.3 `bot_orders` table — no changes

Hedge child uses standard `order_type` values: `'entry'`, `'tp'`, `'close'`. The `order_type` values `'hedge'` and `'hedge_tp'` are deprecated and will not be written after v3.6.0.

---

## 4. Lifecycle State Machine

```
PARENT BOT (LONG)                    HEDGE CHILD BOT (SHORT)
─────────────────                    ───────────────────────
Scanning                             hedge_standby
  │                                      │
  │ entry fills step 1..N               │ (dormant, is_active=1 but no orders)
  ▼                                      │
IN TRADE step N                          │
  │                                      │
  │ step >= hedge_trigger_step           │
  │ AND no hedge child active            │
  ├──── signal: place_hedge_entry ──────►│
  │                                      ▼
  │                                  IN TRADE step 1
  │                                  (SHORT entry at parent's avg_entry_price)
  │
  │ step N+1 fills                       │
  ├──── signal: place_hedge_entry ──────►│ step 2 entry placed
  │     (qty = martingale step size)     │ (SHORT entry at step N+1 fill price)
  │
  │ ... continues for each new step      │ ... child accumulates SHORT position
  │
  │ TP hits                              │
  ▼                                      │
Scanning (cycle reset)                   │ receives: parent_tp_signal
  │                                      ▼
  │                                  Place break-even TP
  │                                  (limit GTC at child's avg_entry_price)
  │                                      │
  │                                      │ TP fills
  │                                      ▼
  │                                  Scanning → hedge_standby
  │◄─────────────────────────────────────┘
  │
  ▼
Next cycle (parent and child both clean)
```

### Status values for hedge child

| Status | Meaning |
|---|---|
| `hedge_standby` | Configured, waiting for parent to trigger |
| `IN TRADE` | Actively holding SHORT position |
| `Scanning` | TP hit, resetting before returning to `hedge_standby` |

The engine sets `hedge_standby` after `reset_bot_after_tp` completes for a hedge child.

---

## 5. Invariants

These must be true at all times after v3.6.0 is deployed. Tests verify each one.

**INV-1:** Every row in `bots` where `bot_type='hedge_child'` has a non-null `parent_bot_id` pointing to an existing `standard` bot.

**INV-2:** Every row in `bots` where `hedge_child_bot_id IS NOT NULL` has a corresponding `bot_type='hedge_child'` row with matching `parent_bot_id`.

**INV-3:** `apply_oneway_entry_cross_reduction()` never modifies `trades.open_qty` of a hedge child bot as a result of its parent bot's fills. Cross-reduction between parent and hedge child is permanently suppressed.

**INV-4:** `apply_oneway_entry_cross_reduction()` continues to apply normally between any two bots that are not in a parent/child hedge relationship.

**INV-5:** `trades.hedge_qty` is 0.0 for all bots after migration. It is never written again.

**INV-6:** `recompute_invested_from_orders()` returns a 4-tuple `(cost, avg, qty, step)`. The fifth element `h_qty` does not exist after v3.6.0.

**INV-7:** A hedge child's `open_qty` is modified only by: (a) `credit_fill()` when the child's own entry orders fill, (b) `credit_fill()` when the child's own TP/close orders fill, (c) `seal_trade_state()` recompute. Never by cross-reduction from parent.

**INV-8:** `update_active_positions_snapshot()` assigns the hedge child's SHORT position to the hedge child bot's `bot_id`. `bot_id=0` (orphan) never occurs for positions owned by hedge child bots.

**INV-9:** The hedge child TP price is: `current_price` if the position is already profitable at time of placement, otherwise `avg_entry_price` (break-even). A profitable SHORT child (`current < entry`) closes immediately at market. A losing SHORT child waits for price to recover to entry price.

---

## 6. Deleted Code (Complete List)

After all tickets are merged, these are removed entirely:

| Symbol | File | Reason |
|---|---|---|
| `get_bot_hedge_qty()` | `database.py:3577` | hedge child has own `open_qty` |
| `basket_open_qty_from_recompute()` | `database.py:3278` | no longer needed |
| `h_qty` return value | `database.py:recompute_invested_from_orders` | 4-tuple replaces 5-tuple |
| `hedge_qty` SQL CASE blocks | `database.py:3316-3329, 3368-3375` | deleted with `h_qty` |
| `order_type='hedge'` handling | `database.py:1105, 1177, 1536` | deprecated order type |
| `order_type='hedge_tp'` handling | `database.py:1105` | deprecated order type |
| `cycle_phase='HEDGED'` | `database.py:3627` | replaced by child `IN TRADE` |
| `cycle_phase='HEDGE_EXIT_PENDING'` | `database.py:1766` | replaced by child placing TP |
| `hedge_qty` write in `seal_trade_state` | `ledger.py:460,532,543` | column deprecated |
| `h_qty` in `seal_trade_state` | `ledger.py:427,433,460,517,543,548` | 4-tuple return |
| `basket_open_qty_from_recompute` import | `ledger.py:432` | function deleted |
| `order_type NOT IN ('hedge','hedge_tp')` guard | `ledger.py:791` | no longer needed |
| `h_qty` in `handle_tp_completion` | `ledger.py:816` | 4-tuple return |
| `execute_hedge_lock()` | `bot_executor.py:3388` | replaced by child bot entry signal |
| `check_hedge_entry` / `calculate_hedge_lot` call site | `bot_executor.py:3359` | replaced by child bot signal |
| `_hedge_cooldown_ts` | `bot_executor.py:84` | no longer needed |
| `HEDGE` special case in `_is_order_net_reducing` | `bot_executor.py` | child bot handles its own reduces |
| hedge net calculation | `reconciler.py:2983,3451,3469,3475,3885` | child bot's own ledger handles this |
| `WIPE-ABORT` hedge guard | `reconciler.py:5344` | child `IN TRADE` status blocks wipe correctly |
| `h_qty` threading | `reconciler.py:7091,7117,7133,7155,7171,7173,7669` | 4-tuple return |
| `get_bot_hedge_qty` import | `parity_gates.py:47` | function deleted |
| hedge subtraction in `get_bot_signed_contribution` | `parity_gates.py:60-63` | child's own `open_qty` is correct |

---

## 7. Migration Plan for Bot 10017 (xrp long)

Bot 10017 currently has:
- `open_qty = 0.0` (no basket position)
- `hedge_qty = 44.7` (net SHORT from hedge fills)
- Physical exchange: 44.7 XRP SHORT, `bot_id=0` in `active_positions`

Migration steps (one-time script `scripts/migrate_hedge_to_child_bot.py`):

1. Create new bot row: `name='xrp hedge'`, `pair='XRP/USDC:USDC'`, `direction='SHORT'`, `bot_type='hedge_child'`, `parent_bot_id=10017`, `is_active=1`, `status='IN TRADE'`
2. Create `trades` row for new bot with `open_qty=44.7`, `cycle_id=1`, `position_side='SHORT'`
3. Insert one `bot_orders` row: `order_type='entry'`, `filled_amount=44.7`, `status='filled'`, representing the net hedge position (audit record)
4. Update `bots` row 10017: set `hedge_child_bot_id=<new_id>`, `hedge_trigger_step=<value from config>`
5. Zero `trades.hedge_qty` for bot 10017
6. Update `active_positions`: set `bot_id=<new_id>` for the XRPUSDC SHORT row
7. Run `seal_trade_state(10017)` and `seal_trade_state(<new_id>)` to verify consistency

---

## 8. New Behaviour: Parent Signalling Hedge Child

When `bot_executor.maintain_orders()` runs for a parent bot and detects `current_step >= hedge_trigger_step`:

```
if no hedge child entry exists for this step:
    fetch child bot via hedge_child_bot_id
    compute entry_qty = size of the step that just filled (from martingale config)
    place SHORT entry order on child bot via normal credit_fill path
    save_bot_order(child_bot_id, 'entry', ...)
```

The child bot then follows exactly the same TP/grid management path as any standard bot, with one difference: it does not scan for entry signals — only the parent signals it.

When parent TP fires (`handle_tp_completion`):
```
if parent has hedge_child_bot_id:
    child_state = get_bot_status(hedge_child_bot_id)
    if child_state['open_qty'] > 0:
        place limit GTC TP on child at child_state['avg_entry_price']
        save_bot_order(child_bot_id, 'tp', ...)
```

---

## 9. Break-Even TP Calculation

The hedge child bot entered SHORT across multiple steps. `seal_trade_state()` computes `avg_entry_price` as the weighted average across all entry fills — this is already correct standard behaviour. The break-even TP is placed at exactly `avg_entry_price`.

```
break_even_price = trades.avg_entry_price  (for the hedge child bot)
```

No new math. No new function. The existing `seal_trade_state()` weighted average is the answer.

---

## 10. One-Way Netting Interaction

`apply_oneway_entry_cross_reduction()` in `oneway_netting.py` must be updated to suppress cross-reduction between a parent bot and its hedge child.

**Correct behaviour (v3.6.0+):**
Parent LONG fills → skips hedge child (parent/child relationship detected) → applies cross-reduction to any other unrelated SHORT bots on the pair normally

**Detection:** In the neighbor query, exclude the filling bot's hedge child:
```sql
AND NOT (b.id = (SELECT hedge_child_bot_id FROM bots WHERE id = <filling_bot_id>))
```

---

### 10.1. Cross-Reduction Race Condition — Stale TP and Physical Orphan (v4.0.1 fix)

**Root cause identified June 10, 2026 (BTC 0.002 orphan):**

The exact failure sequence:
1. `short btc` places TP for 0.044 BTC at 23:46:48
2. `long btc price` entry fills 0.002 BTC at 23:46:52 — cross-reduction fires, reduces `short btc` virtual `open_qty` from 0.044 → 0.042; zeros `long btc price` virtual `open_qty` to 0.0
3. `short btc` TP fills at 23:46:55 for the **old qty 0.044** — but physical is now only 0.042
4. 0.044 − 0.042 = **0.002 BTC over-close** → account goes net LONG +0.002
5. Both bots are virtually flat → orphan has no owner

**Two bugs, one race:**

**Fix A (INV-28A) — Stale TP Cancellation.** When `apply_oneway_entry_cross_reduction` reduces a sibling bot's `open_qty`, any resting TP or `dust_close` order for that sibling is now over-sized. The function immediately cancels the resting order on the exchange (`exchange.cancel_order()`) and marks it `cancelled` with notes `[CROSS-REDUCE-CANCEL]` in `bot_orders`. The next `maintain_orders` cycle for the sibling places a correctly-sized replacement TP. This is the **primary fix** — it prevents the over-close entirely.

**Fix B (INV-28B) — Physical Orphan Check.** When `apply_oneway_entry_cross_reduction` zeros the *filling* bot's virtual `open_qty` (ONEWAY_CROSS_SRC row), the function calls `get_exchange_signed_net()` to verify the physical position. If a physical long position > 0.0001 remains, the filling bot is immediately transitioned to `status='pending_flatten'`, triggering `_handle_pending_flatten` in `runner.py` to close it.

**Exchange access from WS thread:** Both fixes require the exchange object on the WS event thread. `credit_fill` now accepts an optional `exchange=` kwarg. The WS handlers pass `BotRunner.get_instance()._local_exchange` (set during startup; falls back to on-the-fly `ExchangeInterface` construction using `config.TESTNET` from the environment if not yet populated).

**Invariants cross-reference:** INV-28A and INV-28B are formally documented in `CODEBASE_GUIDE.md §3.48`.

---

## 11. Implementation Tickets

See `TICKETS.md` in this package. Tickets must be implemented in order. Each ticket is independently testable before the next begins.

---

## 12. Test Scenarios

See `TEST_SCENARIOS.md` in this package.
