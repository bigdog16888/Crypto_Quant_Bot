# Crypto Quant Bot — AI Agent Codebase Guide
**Version: 3.1.4 | Last Updated: 2026-05-12**

> **READ THIS FIRST** before touching any code. This is the single authoritative guide.
> It supersedes `UNIFIED_BOT_DOCUMENTATION.md` and all older session notes.
> Every invariant here was added because someone violated it and the system broke.

---

## 🏗️ High-Level Engine Architecture
The Crypto Quant Bot relies on a **Proof-Only Reconciliation Architecture**. 
1. **Virtual Ledger (`crypto_bot.db`):** The system tracks exactly what each bot intends to hold through cryptographic proof (clientOrderIds like `CQB_###`).
2. **Physical Imprint (Binance):** The system connects to Binance via CCXT & Raw FAPI to verify actual holdings.
3. **The `StateReconciler`:** Compares the DB math against the physical imprint. If any impossible discrepancy occurs (e.g. Virtual > Physical), the system will NEVER invent numbers or guess (as it did historically across heuristical patching). It will default to the **Market Flatten Protocol**, physically wiping the exchange position safely to $0 and neutralizing the bot ledger to Step 0 to guarantee mathematical safety. No orphaned dust will survive.

---

## ⚠️ RULE #0 — ONE-WAY MODE (Read before touching ANY order or position code)

```
THE BINANCE ACCOUNT IS IN ONE-WAY MODE.  NOT HEDGE MODE.  NEVER HEDGE MODE.
```

This is the most fundamental fact about this system. Every API mistake so far has come from ignoring it.

### What One-Way Mode means on the exchange

| Fact | Detail |
|------|--------|
| **One net position per symbol** | The exchange keeps a SINGLE position per symbol. Positive = net LONG, negative = net SHORT. |
| **Long + Short bots net out** | Bot A buys 100 SUI (LONG bot) and Bot B sells 80 SUI (SHORT bot) → exchange shows +20 SUI net LONG. |
| **No separate LONG/SHORT legs** | There is no "SUI LONG position" AND "SUI SHORT position" on the exchange. Only one net number. |
| **positionSide is always 'BOTH'** | The raw Binance API response always has `positionSide: "BOTH"` in one-way mode. It carries zero directional information. |

### What this means for code

| Action | Correct | Wrong (hedge mode) |
|--------|---------|-------------------|
| Determine direction from exchange | Use **sign of `positionAmt`** (+ = LONG, − = SHORT) | Read `positionSide` field |
| Close a LONG position | `side=sell, reduceOnly=True` | `positionSide=LONG` |
| Close a SHORT position | `side=buy, reduceOnly=True` | `positionSide=SHORT` |
| Any order placement | **Never send `positionSide`** | Sending `positionSide=LONG/SHORT` → Binance 400 error |

### What this means for virtual tracking (our DB)

The system's virtual LONG/SHORT per-bot tracking in `trades` and `bot_orders` is our **internal accounting layer only**. It does NOT map to hedge-mode positions. Multiple bots can be LONG or SHORT on the same pair simultaneously — their real effect nets on the exchange.

---

## 1. Project Layout

```
Crypto_Quant_Bot/
├── engine/
│   ├── runner.py              ← Main bot loop, cycle orchestration, snapshot mgmt
│   ├── bot_executor.py        ← Per-bot order execution (Entry, Grid, TP logic)
│   ├── reconciler.py          ← Offline fill detection & state recovery
│   ├── integrity.py           ← FLAG-ONLY mismatch detection (does NOT mutate state)
│   ├── database.py            ← All SQLite operations (single source of truth layer)
│   ├── exchange_interface.py  ← CCXT + raw Binance FAPI wrapper
│   ├── ws_cache.py            ← In-memory position/order snapshot (WS + REST merged)
│   ├── ws_event_handlers.py   ← Real-time WebSocket fill processing
│   └── websocket_handler.py   ← WS connection manager
├── ui/app.py                  ← Streamlit dashboard entry point
├── ui/views/monitor.py        ← Live monitor & mismatch display
├── config/settings.py         ← Config loader (.env → config object)
├── crypto_bot.db              ← Live SQLite database (WAL mode)
├── engine.log                 ← Rotating log (10MB, 5 backups)
└── restart_runner.bat         ← Kills + restarts the engine process
```

---

## 🎨 UI Architecture — Native Fragments
The dashboard (`monitor.py`) utilizes **native Streamlit Fragments** (`@st.fragment`) for auto-refresh.
- **Header Metrics**: Refreshes every 30s (Equity, Balance, PnL).
- **Bot Grid**: Refreshes every 15s (Steps, Invested, Status).
- **Rule**: Avoid external refresh components (like `st_autorefresh`) as they fail in restricted network environments. Native fragments are the absolute standard.

---

## 2. Database Schema — The ONLY Authoritative Reference

These tables have STRICT rules about who writes them and what they mean.
**Violating these rules is the #1 source of all position discrepancy bugs.**

### `bots`
Config table. One row per bot. Never used for position state.
Key fields: `id`, `pair`, `direction` (LONG/SHORT), `is_active`, `status` (display-only string).

### `trades`
**Virtual ledger** — what the system *believes* the bot holds.
Key fields: `bot_id`, `total_invested`, `avg_entry_price`, `current_step`, `cycle_phase`, `position_side`, `entry_confirmed`, `cycle_id`.

> **RULE**: `trades.total_invested` MUST always be derived from `bot_orders` filled rows via
> `recompute_invested_from_orders()`. It must NEVER be set by direct SQL UPDATE except through
> the authorized functions (`reset_bot_after_tp`, `safe_wipe_bot`).
> Any code that does `UPDATE trades SET total_invested = X` directly is wrong.

### `bot_orders`
**The authoritative proof ledger.** Every order ever sent to the exchange.
Key fields: `bot_id`, `client_order_id` (CQB_ prefixed), `order_type`, `status`,
`filled_amount`, `price`, `position_side`, `cycle_id`, `step`.

> **RULE**: `client_order_id` is the proof of ownership. Format: `CQB_{bot_id}_{type}_{step}_{ts}`.
> Any reconciliation or adoption that cannot produce a matching CQB_ ID is synthetic and forbidden.

> **RULE**: `position_side` must be one of `'LONG'`, `'SHORT'`, `'BOTH'`, or `NULL`.
> Rows with `position_side='BOTH'` indicate pre-hedge-mode fills and must be treated as matching
> either side (use `OR position_side IS NULL OR position_side='BOTH'` in all queries).

### `active_positions` ⚠️ MOST MISUSED TABLE
**Exchange reality snapshot.** Written exclusively by `update_active_positions_snapshot()`.
Key fields: `bot_id`, `pair`, `side` (LONG/SHORT), `size`, `entry_price`, `last_checked`.

> **RULE — BROKEN HISTORICALLY, NOW FIXED (v1.7.0)**:
> This table must contain ONLY exchange-sourced data from `fetch_positions()`.
> It must be fully replaced (DELETE + INSERT) on every snapshot call.
> **NEVER** write virtual ledger values into this table. `update_active_positions_for_bot()`
> (which writes `total_invested / avg_entry` into this table) contaminates it with virtual data
> and must not be called in the fill path. The full-replacement approach is the fix.

> **RULE — HEDGE MODE DEDUP KEY**:
> In Binance Hedge Mode, `fetch_positions()` returns two entries per symbol — one LONG and one SHORT.
> The dedup key MUST be `(symbol, positionSide)` not `(symbol, p.side)`.
> `p.side` is the ORDER direction ('buy'/'sell'). `positionSide` from `p['info']` is the
> POSITION side ('LONG'/'SHORT'). Using `p.side` as the key caused SHORT positions to be merged
> with LONG, producing half the actual size (e.g., SUI SHORT 168.2 appearing as 84.1).

### `trade_history`
Immutable closed trade archive. Written only by `reset_bot_after_tp()`. Never read for position logic.

### `reconciliation_logs`
Append-only audit log for all reconciler actions. Never delete rows.

---

## 3. Critical Architectural Invariants

**Every invariant below was added because someone violated it and the system broke.**

### 3.1. Proof-Only Consensus
- The engine only credits fills that have a matching `CQB_` `clientOrderId` in `bot_orders`.
- **No synthetic adoptions**: If a position exists on the exchange but cannot be matched to a
  `bot_orders` fill by CQB ID, it is an orphan. It gets `bot_id=0` in `active_positions` and
  surfaces in the monitor as a `[REALITY-ORPHAN]`. It is NOT automatically adopted.
- The ONLY authorized exception is `DUST_CHASER` (sub-$5 positions) for Binance min-notional.

### 3.2. Gross-Directional Tracking (not netted)
- **NEVER** compute `Exchange_Net - Virtual_Net` across directions.
- Compare LONG vs LONG, SHORT vs SHORT, independently.
- Example: LONG +$100k and SHORT -$100k = $0 net. This is NOT a mismatch. Equal opposing bots.

### 3.3. Symbol Normalization
- Always use `normalize_symbol(sym)` from `exchange_interface.py`.
- Binance REST/CCXT: `"BTC/USDC:USDC"` (slash + margin suffix).
- Binance WebSocket: `"BTCUSDC"` (no slash, no suffix).
- `ws_cache` normalizes all keys. Bypassing this creates phantom positions.

### 3.4. Order Isolation (Multi-Bot Rule)
- **NEVER call `cancel_all_orders(pair)`** in bot logic.
- Always use `cancel_orders_by_bot_id(bot_id, pair)`.
- Every order is tagged `CQB_{bot_id}_{type}_{step}_{uuid}` as `clientOrderId`.

### 3.5. `reduceOnly` is Pair-Level, Not Bot-Level
- `reduceOnly=True` on TP is ONLY safe when exactly **1 bot** is active on a pair.
- With >1 bots on a pair, Binance returns `-2022 ReduceOnly Order is rejected`.

### 3.6. `safe_wipe_bot()` is the ONLY Authorized Reset Path
- **NEVER** call `reset_bot_after_tp(bot_id, ..., action_label='SYSTEM_WIPE')` directly.
- ALL destructive resets go through `safe_wipe_bot(bot_id, pair, direction, reason, exit_price)`.
- 3 guards: `CARRY_PENDING` phase blocks wipe, physical qty > 0.0005 blocks wipe, ledger net qty > 0.0005 blocks wipe.
- **Python scoping trap**: NEVER `import safe_wipe_bot` inside a function body. The import is at
  the top of `reconciler.py`. Inline import makes Python treat it as a local variable for the
  ENTIRE enclosing function, causing `UnboundLocalError` at every earlier reference.

### 3.7. `cycle_phase` State Machine
Column `trades.cycle_phase`, default `'ACTIVE'`.
Transitions:
- `ACTIVE` → `CARRY_PENDING`: TP hit with residual carry quantity
- `ACTIVE` → `IDLE`: Clean TP hit with zero remaining quantity
- `IDLE` → wiped (if ledger=0 AND physical=0, via `_align_memory_to_ledger`)
- `CARRY_PENDING` → `ACTIVE`: Carry fills confirmed in next cycle

> **RULE**: `CARRY_PENDING` bots are NOT ghosts. `safe_wipe_bot()` guard 1 blocks their reset.
> `IDLE` bots with `entry_confirmed=1` AND zero ledger AND zero physical WILL be auto-reset by
> `_align_memory_to_ledger` (v1.7.0 fix). Previously they accumulated as ghost positions forever.

### 3.8. `heal_cycle_fragmentation` uses CQB Proof, Not Cycle Numbers
- Only migrate `bot_orders` rows where `cycle_id IS NULL` and `client_order_id LIKE 'CQB_%'`.
- **NEVER** migrate rows where `status IN ('new', 'open')` — these are standing live exchange
  orders. Their `cycle_id` is ground truth. Moving them corrupts the cycle they belong to.
- The correct proof of ownership is the CQB ID, not a numeric cycle comparison.

### Version 1.8.3 (API Stabilization & Sync Flow Documentation)
- **Demo FAPI Price Bug & Artificial TP Loss**: Identified and documented the edge case where the Binance Demo FAPI suppresses the `avgPrice` field on completed limit orders. Combined with illiquidity wick events, this forced `actual_exit` to fall back to the live `current_price` at that exact millisecond. If the price dropped, this recorded an artificial loss (e.g. SUI logging a -0.19 PNL on a TP hit). The recent FAPI parse fix in `exchange_interface.py` prevents this future corruption.
- **WebSocket Position Streaming Delay**: Documented standard startup synchronization behavior. When starting the WebSocket monitor, initial exchange balances will momentarily read `0.0` until the first `position_update` event streams from the exchange.
- **Standard Operating Procedure**: The documented startup flow is now explicitly defined as `Pre-Sync (REST pull to establish baseline)` -> `Start Monitor (WebSocket stream to maintain live delta)`.

### 3.9. Ledger Mathematics — Canonical Form
All position calculations MUST use:
- **Entries** (add to position): `order_type IN ('entry', 'grid', 'adoption_add', 'adoption', 'carry')`
- **Exits** (subtract from position): `order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl', 'virtual_netting')`
- **Audit-only** (zero ledger impact): `order_type = 'drift_note'`

Any deviation creates ghost balances or zero-out errors.

> **RULE — `drift_note` is the ONLY safe audit record type** (added v3.1.4).  
> `get_pair_virtual_net()` only counts rows where `filled_amount > 0` AND `order_type` matches an Entry or Exit above.  
> `drift_note` rows are always written with `filled_amount = 0`, so they fall through to `ELSE 0` in the accounting SQL.  
> **NEVER use `adoption`, `adoption_add`, or any Entry/Exit type for reconciliation notes** — they will be counted as real fills on the next cycle and cause runaway ledger inflation (the XRP 1 063 → 17 M explosion, observed May 2026).

### 3.10. `position_side` Filter Must Be NULL-Tolerant
```sql
AND (bo.position_side = ? OR bo.position_side IS NULL OR bo.position_side = 'BOTH' OR bo.position_side = '')
```
This handles pre-hedge-mode fills tagged with `'BOTH'` and fills from systems that didn't set this field.
Using strict `AND bo.position_side = ?` silently returns 0 fills for these rows, triggering incorrect
CARRY fallback that reads stale `trades.avg_entry_price` values.

### 3.11. TP Reset Double-Execution Guard
Wrap all TP reset logic with `if total_invested > 0:`. Without this, the REST polling loop can
race against the WS fill handler and fire twice, producing phantom `$0.00 TP_HIT` journal entries.

### 3.12. Early Exit (EE) Decay — Architecture (Correct)

EE decay is a **step function**, NOT continuous per-cycle drift.

All production bots use `DecayIntervalMins` + `DecayPercentPerInterval` (configured in the bot UI).
The formula in `manager.calculate_early_exit_decay` (line 46) uses:
```python
intervals_passed = math.floor(duration_mins / interval_mins)  # INTEGER floor
ee_pc += intervals_passed * decay_per_interval
```
`math.floor` makes this a staircase: the TP value is **identical** between interval boundaries
and **steps down** only when a complete interval has elapsed (e.g., every 15 minutes).

The `EEHoursPC` (linear per-hour) mode is NOT used by any current bot — it would cause continuous
per-cycle drift. Do not enable it without understanding the SYNC-DRIFT implication (see §5 table).

`basket_start_time` MUST be updated to `int(time.time())` in `accumulate_trade_fill` on every
limit fill (both entries AND grid averages). Without this, the decay anchors to the original
cycle open time, crashing fresh grids toward break-even prematurely.

### 3.13. Carry-Over Ghost Mass Protection
Administrative exits (`SYSTEM_WIPE`, `MANUAL_CLOSE`, `STOP_LOSS_EXIT`) must NEVER trigger carry-over.
`reset_bot_after_tp` uses `action_label` to detect admin exits and skip carry propagation.

### 3.14. Decimal Precision Guardian (v1.9.4)
All trading mathematics, specifically price and quantity rounding, MUST use `decimal.Decimal` with fixed-point arithmetic. 
- **Rule**: Initialize all decimals using string serialization: `Decimal(str(value))`. This strips absolute binary floating-point noise (e.g., `.699999999`) and restores the intended human-readable value.
- **Rule**: `math.floor` and `math.ceil` are forbidden for lot-size and price calculations. Use `exchange.round_to_step()` and `exchange.ceil_to_step()` which encapsulate the Decimal quantize engine.

### 3.15. Succession Proof — 99% Milestone Rule (v1.9.1)
To ensure the Virtual Ledger remains the "Absolute Ground Truth," bot steps only progress to the next grid level when the current step's fill probability is effectively certain.
- **Rule**: `current_step` only advances if `filled_amount / target_amount >= 0.99`.
- **Reasoning**: This prevents the bot from "calculating forward" on partial fills, ensuring that the ledger and exchange stay in locked parity.

### 3.16. One-Way Mode Residue Consolidation (The "Finished State" Gate)
In One-Way mode, residues on the opposite side of the physical net cannot be closed via exchange orders (ReduceOnly rejection). These are neutralized via the **Consolidation Protocol**:
1. **Dynamic Dust Gate**: A position is "Dust" ONLY if `abs(qty * price) < symbol_min_notional` OR `abs(qty) < symbol_min_qty` (queried from exchange metadata).
2. **Phase Gate**: Only bots in `Scanning` status or `cycle_phase = 'IDLE'` are eligible. This ensures active trading bots are never wiped.
3. **Action**: The trapped residue is neutralized via `safe_wipe_bot(reason='CONSOLIDATION')`.
4. **Healing**: The Reconciler detects the resulting gap and executes an **Adoption-Reduce** on the primary (physical-side) bot.

### 3.17. Hedge Integrity & Gross-Exposure Gating
To protect fully hedged bots (net quantity = 0 but gross invested > 0), the system uses **Gross-Exposure Gating** for all state transitions:
- **`seal_trade_state`**: Status is only flipped to `Scanning` if `total_invested` (gross) is effectively zero.
- **`entry_confirmed`**: Remains `1` if any proven fill exists for the current cycle, regardless of whether it was later offset by a hedge.
- **`sync_trades_from_orders`**: If a bot is fully hedged, the logic preserves the `HEDGED` phase and `entry_confirmed=1` status, ensuring the bot doesn't "DNA-WIPE" while physically active.

### 3.18. Fractional Drift Sweeper — `drift_note` Protocol (v3.1.4)

When the autonomous 48 h forensic fill scan (AUTONOMOUS-HEAL) still cannot close the gap between the system ledger and the physical exchange position, the engine applies a two-tier outcome:

| Residual gap | Action |
|---|---|
| `qty ≤ 0.5 units` **AND** `USD ≤ $5.00` | Write a `drift_note` row (`filled_amount=0`) per bot as an audit trail. Bots stay in their current status. |
| Any gap exceeding either threshold | Set all bots on that ticker to `REQUIRE_MANUAL_PROOF` for human review. |

**Why the thresholds?**  
0.5 units catches rounding dust on high-price assets (0.5 BTC would be ~$30 k and would fail the $5 USD check). $5 catches dust on cheap tokens without letting material gaps slip through.

**Audit trail structure (`bot_orders` row):**
```
order_type    = 'drift_note'   ← safe — NOT in any Entry/Exit set
filled_amount = 0              ← passes the `AND filled_amount > 0` guard → zero impact
status        = 'audit'        ← will never be processed as a live order
client_order_id = CQB_{bot_id}_DRIFT_{symbol}_{ts}
notes         = full diagnostic string for future audits
```

---

## 4. Reconciler Architecture

### Startup Sequence (in order)
1. `prime_startup_snapshot()` — fetches ALL exchange positions ONCE, writes `active_positions`
2. `reconstruct_offline_fills(48h)` — credits any fills that happened while offline
3. `_align_memory_to_ledger()` — syncs `trades.total_invested` from `bot_orders` ledger
4. `resolve_net_mismatch()` — surface-level mismatch flagging (does not auto-fix)
5. `run_cycle()` — begins normal polling

### Periodic Reconciliation
- **Every ~10 cycles**: `reconstruct_offline_fills(2h)` — fast lookback for recent fills
- **Every 60 cycles**: `reconcile_all()` — full reconciliation on persistent instance

### Three-Pass Adoption Logic (`adopt_from_physical_positions`)
| Pass | Method | Guard |
|------|--------|-------|
| PASS 1 | Match by `clientOrderId` from exchange fill history | CQB_ prefix + bot_id match |
| PASS 2 | Match by order_id cross-reference in `bot_orders` | `order_id` exact match |
| PASS 3 | Forced adoption when `history_restricted=True` | **bot.direction must == physical.side** (v1.7.0 fix) |

> **PASS 3 direction guard** (v1.7.0): Before calling `inject_adoption_row`, verify
> `bot.direction.upper() == side.upper()`. Violation caused SHORT SUI inventory to be injected
> into the LONG SUI bot's ledger, creating phantom 79.8 SUI positions.

---

## 5. Common Failure Patterns — Definitive Reference

| Symptom | Root Cause | Correct Fix |
|---------|-----------|-------------|
| Monitor shows `system=0` for SHORT bots | `trades.total_invested=0` because `bot_orders` has zero fills for those bots, OR `position_side` filter excluded `'BOTH'`-tagged rows | Check bot_orders for filled rows; apply NULL-tolerant position_side filter (invariant 3.10) |
| `active_positions` shows half the real qty | Hedge-mode dedup used `p.side` instead of `positionSide`; merged LONG+SHORT | Fixed in v1.7.0: use `p['info']['positionSide']` as dedup key |
| IDLE bots stuck with `total_invested > 0` forever | `_align_memory_to_ledger` `DNA-HOLD` guard blocked resets for `entry_confirmed=1` bots even when IDLE+zero-physical | Fixed in v1.7.0: check `cycle_phase=IDLE` AND physical=0 to bypass hold |
| `SIZE DISCREPANCY` in logs | `ws_cache` has duplicate symbol keys | Check `normalize_symbol()` is called before every `ws_cache` lookup |
| `-2022 ReduceOnly Order rejected` | `reduceOnly=True` on a multi-bot pair | Check sibling bot count in `bot_executor.py` before flagging reduceOnly |
| Runaway `adoption_add` rows (phantom inflation) | PASS 3 adopted SHORT position into LONG bot (no direction guard) | Fixed in v1.7.0: PASS 3 direction guard |
| SOL/other bot TP orders from old cycle visible | `heal_cycle_fragmentation` migrated `status='new'` orders across cycles | Fixed in v1.7.0: only migrate NULL-cycle filled rows with CQB proof |
| BNB CARRY reads wrong qty (0.05 instead of 0.04) | `position_side='BOTH'` rows excluded by strict filter → Pass 1 returns 0 → fallback uses stale `trades.avg_entry_price` | Fixed in v1.7.0: NULL-tolerant position_side filter in Pass 1 |
| `[ADOPTION_BLOCKED]` everywhere | Sibling bots claimed full position value | Expected behaviour — prevents double-counting on shared pairs |
| `UnboundLocalError: safe_wipe_bot` | `import safe_wipe_bot` inside a function body | Remove inline import — use file-top import in reconciler.py only |
| Bot resets immediately after CARRY TP | `safe_wipe_bot()` guard 1 not firing | Check `trades.cycle_phase` is set to `CARRY_PENDING` by `reset_bot_after_tp` |
| Orphaned physical position with no system entry | Position opened manually or by a reset-then-deleted bot | Use Force SL from Bot Manager UI to cleanly close the position |
| XRP/SUI/ETH SHORT with system=0 | Bots have zero `bot_orders` fills — position was never opened by the bot system | Must use Force SL via Bot Manager to close; cannot be adopted without CQB proof |
| "MISSING GRIDS" alert persists despite grid on exchange | `get_bot_order_ids()` only queried `status='open'`; Binance FAPI returns `status='new'` for acknowledged orders → `local_grid_ids=[]` → engine re-places grid indefinitely | Fixed in v1.8.2: query includes `'new'` and `'placing'` statuses |
| `trades.tp_order_id` stuck as `PLACING_CQB_...` forever | Pre-commit pattern writes placeholder to `trades` but `update_bot_order_exchange_id` only updated `bot_orders`, leaving the stalemate evictor querying a non-existent exchange ID | Fixed in v1.8.2: `update_bot_order_exchange_id` now back-fills `trades` table; `get_bot_order_ids` strips `PLACING_` and self-heals from `bot_orders` |
| `[SYNC-DRIFT]` fires every cycle replacing a valid TP | Drift-check re-computed `db_tp` via `_compute_effective_tp(avg_entry_price)` each cycle. If `avg_entry_price` shifted (grid fill), the re-computed base TP differed from the placed TP → false drift even when no EE interval elapsed | Fixed in v1.8.4: drift-check reads `bot_orders.price` (what was physically placed on Binance) and compares it to the live exchange TP. EE interval change is detected separately via `new_ee_tp != placed_tp`. Tolerance reduced from 2% (patch) to 0.1% (tick-size rounding only) |
| `python-dotenv could not parse` on startup (lines 17-51) | A test exercised the UI "Apply Settings" path with a mocked `st.text_input()` that returned a `MagicMock` object. `set_key()` wrote `MagicMock repr` verbatim to `.env`. The `<` character in the repr is illegal in dotenv format | Fixed in v1.8.4: `ui/app.py` validates inputs before writing (len≥10, printable ASCII, no `<`). `.env` now has stub `BINANCE_API_KEY=` so `set_key()` updates in-place, never appends |
| Virtual net explodes to millions (XRP 1k→17M) | Forensic adoption wrote `adoption_add` rows which `get_pair_virtual_net()` counted as real fills, doubling the gap each cycle and triggering a new adoption record | Fixed in v3.1.0: Bidirectional forensic adoption disabled. v3.1.4 adds fractional drift sweeper using `drift_note` (invisible to ledger math) for sub-$5 / sub-0.5u residuals |
| Monitor `Trigger` column shows `N/A` for all scanning bots | `extract_info` computed price-threshold proximity only; RSI/CCI branches existed but returned the raw level string without live value | Fixed in v3.1.2: `get_indicator_val()` fetches live OHLCV and computes RSI/CCI/Stoch in-fragment with a local cache; proximity label (`🟢 IN RANGE / 🟡 SOON / ⚪ FAR`) attached to every trigger type |
| `💥 Close` on mismatch row returns Binance 400 ReduceOnly error, bot stays stuck | `create_order(reduceOnly=True)` rejected because position already flat; error aborted the bot-state reset | Fixed in v3.1.3: `Close` now catches `reduceOnly`/`400`/`not found` errors, issues a warning toast, and **always** proceeds to `reset_bot_after_tp` so bot returns to IDLE even when exchange is already flat |

---

## 6. How to Restart Safely

```powershell
# Stop and restart engine
.\restart_runner.bat

# Start UI (separate terminal)
streamlit run ui/app.py

# Tail log
Get-Content engine.log -Wait -Tail 30
```

After restart, watch for:
- `[SNAP] active_positions refreshed: N owned + M orphans` — exchange positions loaded
- `[DNA-ALIGN]` — memory aligned to ledger
- `[PHYS-ADOPT]` — physical adoption running (should NOT crash with NameError anymore)

---

## 7. Version History (Change Log)

### v3.1.4 — 2026-05-12
**Fractional Drift Sweeper + `drift_note` Protocol + CODEBASE_GUIDE refresh.**

**reconciler.py**:
- `resolve_net_mismatch` / PASS 3 final block: replaced the binary "forensic scan failed → REQUIRE_MANUAL_PROOF" escalation with a two-tier outcome. Residual gaps ≤ 0.5 units AND ≤ $5 USD are now auto-resolved by writing a `drift_note` audit row (`filled_amount=0`, type not in any Entry/Exit set → zero ledger impact). Larger gaps still escalate to `REQUIRE_MANUAL_PROOF`.
- `drift_note` rows carry full diagnostics in the `notes` column (proved qty, physical qty, gap, price) for future auditing.

**CODEBASE_GUIDE.md**:
- Version bumped 3.0.9 → 3.1.4.
- §3.9: Added `drift_note` as the safe audit-only type; added `carry` and `virtual_netting` to complete the Entry/Exit sets.
- §3.18: New section — Fractional Drift Sweeper protocol, thresholds, and `bot_orders` record structure.
- §5: Added three new failure patterns (runaway adoption inflation, N/A trigger proximity, ReduceOnly Close stuck).
- §7: Full v3.1.x changelog added below.

### v3.1.3 — 2026-05-12
**Robust Mismatch Recovery `Close` button + Stoch/Bollinger proximity labels.**

**monitor.py**:
- Mismatch section `💥 Close` button: rewritten to call `ex.close_position()` instead of raw `create_order(reduceOnly=True)`. Error handler now explicitly catches `reduceonly` / `400` / `not found` strings and issues a `st.warning` toast instead of aborting. **Bot state is always reset** (`reset_bot_after_tp` + manual_whitelists DELETE) regardless of exchange outcome.
- `extract_info`: Added Stochastic (`mode_stoch`) and Bollinger Band (`mode_boll`) proximity branches with live OHLCV fetch, matching the RSI/CCI pattern.
- In-trade trigger label: bots with no TP and no Grid orders now show `⚠️ NO ORDERS (Adopted?)` instead of silent `In Trade (desc)`.

### v3.1.2 — 2026-05-11
**Real-time Trigger Proximity Intelligence in Dashboard.**

**monitor.py**:
- `extract_info`: Added `get_indicator_val(pair, tf, itype)` inner function with a per-fragment `indicator_cache_f` dict. On each 15 s fragment refresh, RSI and CCI are calculated from live OHLCV (falling back to `market_cache.json` file cache first).
- Proximity labels added for all trigger modes: `🟢 [IN RANGE]`, `🟡 [SOON]`, `⚪ [FAR]` for both price-threshold and oscillator conditions.
- In-trade enrichment: When a bot has a live TP order, `Trigger` column shows `🟢/🟡/⚪ TP Proximity: X.X%` instead of a raw indicator string.
- `physical_order_counts` now pre-computed before `extract_info` is called (resolves the `free variable referenced before assignment` scoping error).

### v3.1.1 — 2026-05-09
**`get_pair_virtual_net` partial-cancel fill inclusion.**

**database.py**:
- `get_pair_virtual_net` SQL: Added explicit handling for `status IN ('canceled','cancelled') AND filled_amount > 0` rows (ATR grid resize cancels old order but it may have been partially filled). These real fills now count toward the virtual net.
- Hedge LIKE clause extended to handle `hedgetp%` prefix variants.

### v3.1.0 — 2026-05-08
**Bidirectional Forensic Adoption Disabled — Proof-Only Escalation.**

**reconciler.py**:
- Removed all `forensic_adoption_add` / `inject_adoption_row` calls from the PASS 3 failure branch.
- Replaced with `REQUIRE_MANUAL_PROOF` status escalation and a detailed comment block (lines ~3595–3619) documenting the exact inflation mechanism that caused XRP 1 063 → 17 M.
- `resolve_net_mismatch` global netting: switched from per-bot `trades.open_qty` snapshot to `get_pair_virtual_net(pair)` as the authoritative system-side quantity, eliminating stale-cache false alarms.

**monitor.py**:
- Mismatch section rewired to call `get_pair_virtual_net` for virtual side (was reading `total_invested` from the DataFrame).
- `physical_order_counts` pre-computation hoisted above `extract_info` definition to fix `UnboundLocalError: free variable referenced before assignment in enclosing scope`.

### v2.0.0 — 2026-04-22
**Major Release: Autonomous Reconciliation Stabilization.**
**Root causes resolved: (1) Deterministic ID parsing fixed infinite loop ghost-sweeping. (2) Pair-Consensus awareness fixed One-Way mode drift alerts. (3) Atomic TP Sync fixed API replacement race conditions. (4) Inline Grid Fill processing cleared DB locks.**

**bot_executor.py**:
- **Deterministic ID Parser**: Replaced tag-based `_GRID_N_` string matching with rigorous `get_step_from_cid` logic. This prevents the Ghost Sweeper from accidentally cancelling valid current-step orders due to string mismatch, which was the source of the infinite "Blocked by Local DB Lock" loop.
- **Pair-Consensus Drift Alert**: Integrated sibling-bot virtual position awareness. In One-Way mode, the engine now calculates expected physical position as `(this_bot_virtual - sibling_bot_virtual)`. This eliminates false-positive drift alerts for hedged pairs (e.g. SUI LONG vs SUI SHORT).
- **Atomic TP Sync**: Implemented `_sync_replace_tp` with state restoration. If a new TP placement fails, the old order is restored to `open` in the DB, preventing the system from "forgetting" the TP and entering an endless sync storm.
- **Inline Grid Fill**: Fills detected during maintenance are now processed immediately via `credit_fill` + `seal_trade_state`, clearing the `local_grid_ids` lock without waiting for the secondary offline reconciler.
- **Null-Tolerant Ghost Sweeper**: Updated all order filters to handle `NULL`, `'BOTH'`, and Empty-string `position_side` values for legacy and one-way compatibility.

**database.py**:
- **Cent-Level Precision**: Standardized all parity checks to a **$0.01 threshold**. This eliminates "Impossible Loop" deadlocks caused by sub-cent floating point discrepancies.
- **Dynamic ID Back-filling**: Enhanced `get_bot_order_ids` to self-heal `PLACING_` placeholders in the `trades` table using `status='new'` confirmations from Binance.

**reconciler.py**:
- **Drift-First Protection**: Grid placement is now atomically blocked if a significant ledger discrepancy is detected, ensuring the bot never "calculates forward" on a corrupt basis.

### v1.8.4 — 2026-04-17
**Root causes: (1) SYNC-DRIFT check re-computed TP from formula instead of reading placed price. (2) `.env` corrupted by test writing MagicMock objects via `set_key()`.**

**bot_executor.py** (fundamental fix):
- `maintain_orders` SYNC-DRIFT block: Removed the pattern of re-running `_compute_effective_tp`
  to get `db_tp` and comparing it against the exchange's live TP price.
  **Root cause**: `_compute_effective_tp` re-calls `calculate_take_profit_price(avg_entry_price)`,
  which produces a fresh float every cycle. When a grid fill shifts `avg_entry_price` between
  cycles, the re-computed base TP differs from the physically placed TP — even if no EE interval
  has elapsed. This falsely triggered `[SYNC-DRIFT]` and the 2% tolerance was a patch hiding it.
  **Correct design**: For the drift check, read `bot_orders.price` (the price actually submitted
  to Binance) as the reference. Compare that against `exchange_tp` (what Binance holds live).
  EE interval change is detected separately: if `_compute_effective_tp` returns a value that
  differs from `placed_tp`, a new interval stepped — then and only then replace the TP.
  Tolerance restored to **0.1%** (covers tick-size rounding only, not formula re-computation).
- Log format: `[SYNC-DRIFT]` now reports which of three conditions triggered the replace:
  `EE-stepped`, `price-drift`, or `qty-drift`, with exact percentages.

**ui/app.py**:
- `Apply Settings` button: Added validation before `set_key()` — value must be str, len≥10,
  printable ASCII, no `<` character (rejects MagicMock repr). If invalid, shows error and
  does NOT touch `.env`. Prevents test artefacts from corrupting the file.

**.env**:
- Cleaned all 24 garbage lines (MagicMock repr written by unconstrained `set_key()` in test).
- Added stub `BINANCE_API_KEY=` and `BINANCE_API_SECRET=` placeholders so `set_key()` updates
  in-place on future writes, never appends duplicate keys.
- Added `RULE-ENV` block to CODEBASE_GUIDE (§8).

**CODEBASE_GUIDE.md**:
- §3.12: Corrected EE decay architecture — documented as step function (not continuous drift).
  Clarified `math.floor` behaviour and why `EEHoursPC` is not used.
- §5: Added `[SYNC-DRIFT]` and `python-dotenv` failure patterns with root causes.

### v1.8.3 — 2026-04-17
**Root causes: (1) `StateReconciler` constructor treated `exchanges={}` as falsy, firing real connections in tests. (2) `error_side` used raw net signs instead of `net_error`, wrong bot wiped. (3) `recompute_invested_from_orders` returned 3-tuple on early exit path. (4) `_fe` UnboundLocalError in race-condition guard.**

**reconciler.py**:
- Constructor: Changed `if exchanges:` to `if exchanges is not None:` — empty dict `{}` is falsy,
  causing real `ExchangeInterface` connections in tests that pass `exchanges={}`.
- `resolve_net_mismatch`: `error_side` replaced with `net_error = virt_net - phys_net` formula.
  Previous logic used raw sign of `virt_net` and `phys_net` independently — failed in hedged
  multi-bot scenarios (LONG ghost + SHORT valid → detected wrong side as ghost).
- Race-condition guard: Moved inside `except` block so `_fe` is always in scope when referenced.
  Previously `_fe` was declared after the try/except it was used in, causing `UnboundLocalError`.

**database.py**:
- `recompute_invested_from_orders`: Early-return path returned 3-tuple; callers expect 4-tuple.
  Changed to `return 0.0, 0.0, 0.0, 0.0`.

### v1.8.4 — 2026-04-20
**Root causes: (1) Hardcoded 1.0 threshold in reconciler wiped small positions. (2) Binance -4061 failure on One-Way mode accounts.**

**reconciler.py**:
- **Structural Ghost Safe-Guard**: Replaced `if phys_qty > 1.0` with `if phys_qty > 0`. This ensures high-value, small-quantity positions (BTC/ETH) are protected from accidental wipes.
- **Precision Alignment**: All qty-gap checks now use a calibrated `QTY_EPSILON` (0.0001) to distinguish between real positions and floating-point noise.

**exchange_interface.py**:
- **One-Way Mode Auto-Repair**: Added a retry mechanism for `Binance -4061` errors. If an account is in One-Way mode, the engine automatically omits `positionSide` and retries, enabling autonomous operation across all account types.

**database.py**:
- **Accounting Side-Tolerence**: Audited and patched `position_side` filters to be NULL/BOTH tolerant, ensuring multi-bot ledger integrity.

### v1.8.2 — 2026-04-16
**Root causes: (1) `get_bot_order_ids` missed grid orders with Binance's `'new'` status. (2) `trades.tp_order_id` permanently stuck with `PLACING_` placeholder.**

**database.py**:
- `get_bot_order_ids`: Grid order query now includes `status IN ('open','new','placing')`. Binance FAPI
  acknowledges limit orders with `status='new'` before confirming them as `status='open'`. Querying only
  `'open'` caused `local_grid_ids=[]` → engine saw no grid in DB + none on exchange → tried to place a
  duplicate grid forever, while the reconciler couldn't fix it because it thought no grid was tracked.
- `get_bot_order_ids`: Added `PLACING_` prefix detection on `trades.tp_order_id`. Pre-commit pattern
  writes `'PLACING_{clientOrderId}'` to `trades` before the exchange call. `update_bot_order_exchange_id`
  only stamped `bot_orders` with the real ID — leaving `trades.tp_order_id` permanently as placeholder.
  The stalemate evictor then called `fetch_order('PLACING_...', pair)` → order-not-found → evicted the
  valid TP, causing unnecessary re-placement. Fix: detect the prefix, look up the real ID from
  `bot_orders`, back-fill `trades`, and return the correct ID.
- `update_bot_order_exchange_id`: Now back-fills `trades.tp_order_id` or `trades.entry_order_id` when
  stamping a real exchange ID onto a `placing` row if `trades` still has the stale placeholder.
- **Accounting Math Fix (`recompute_invested_from_orders`)**: Previously, LEDGER-SYNC subtracted `(sold_qty * exit_price)` from the `total_cost` basis when aggregating position metrics. If the exit price (like a partial TP) was different from entry, this algebraically corrupted the remaining Average Entry Price, sometimes making it negative. This caused the Reconciler to see a mismatch between active_positions and trades, triggering a phantom `adoption_reduce`, which further pushed the ledger negative and trapped the bot in an endless DB-reset loop. The SQL query was fundamentally rewritten to strictly separate `bought_cost` and `bought_qty` from `sold_qty`. `avg_entry_price` is now derived *only* from entries (`bought_cost / bought_qty`), and `total_cost = remaining_qty * avg_entry_price`.
- **EE Decay Reset on Limit Fill (`accumulate_trade_fill`)**: Restored logic where `basket_start_time` is accurately reset to `time.time()` via the accumulator every time a dynamic grid order fills. This ensures Early Exit (EE) calculations correctly track decay strictly out from the most recent grid fill (cost basis shift) rather than anchoring back to the initial bot cycle opening tick.

**bot_executor.py**:
- Grid stalemate evictor (`maintain_orders`): When `fetch_order` confirms a grid is `filled`, the fill
  is now processed **inline** via `accumulate_trade_fill` + `update_order_status('filled')`. Previously,
  the code said "Offline sync will handle this" and returned without clearing `local_grid_ids`. Since
  `reconstruct_offline_fills` is not called frequently enough in the periodic reconciler, the bot looped
  indefinitely in "Blocked by Local DB Lock" state — ledger never updated, mismatch alert never cleared.
  To revert: restore the `elif status_str in ['filled','closed']` branch to log-only + return.
- Inline fill price fallback: Binance Demo FAPI returns `average=0` for filled orders. Added fallback:
  first looks up `bot_orders.price` for the order ID, then falls back to `current_price`. Zero-price
  fills are now rejected with a guard (`if actual_fill_qty <= 0 or actual_fill_price <= 0`) instead
  of silently writing `$0` into the ledger and corrupting `avg_entry_price`.
- Inline fill step extraction: Demo FAPI may omit `clientOrderId` in filled order responses. Added
  DB fallback: if `clientOrderId` is missing or doesn't contain `_GRID_`, queries `bot_orders` for
  the stored `client_order_id` by exchange `order_id` to extract the martingale step number.

**exchange_interface.py** ← **Root Cause**:
- `fetch_order` Demo FAPI path returned only `{id, status, filled, amount}` — silently dropping
  `avgPrice` and `price` from the raw Binance response. This meant every caller of `fetch_order`
  in Demo mode received `order['average'] = None` → fallback to `0` → zero-price fill.
- Fix: the response dict now includes `average` (from `avgPrice`, falling back to `price` if 0),
  `price`, and `clientOrderId`. For limit grid orders on Demo FAPI, `avgPrice` is always `"0"`;
  the `price` field (the limit price) is the correct fill price and is now used as the fallback.
  The `clientOrderId` inclusion eliminates the extra DB lookup for step extraction.

### v1.8.1 — 2026-04-16
**Root cause: `active_positions` was a split-brain table with stale virtual data.**

**database.py**:
- `update_active_positions_snapshot`: Full table replacement (DELETE + INSERT) on every call.
  Previously only wrote `bot_id=0` orphan rows; bot-owned rows were stale virtual-ledger values.
  Now all rows come from live exchange data. Hedge-mode dedup key changed from `(symbol, p.side)`
  to `(symbol, positionSide)` — fixes SUI SHORT and all other SHORT positions showing as half their real size.
- `recompute_invested_from_orders` Pass 1: NULL-tolerant `position_side` filter.
  Previously `AND position_side = ?` excluded `'BOTH'`-tagged fills → triggered wrong CARRY fallback.

**reconciler.py**:
- `adopt_from_physical_positions`: Fixed `NameError: f_side` (was `f_pos_side`) — entire adoption
  loop was silently crashing every reconcile cycle. Primary cause of all unresolved orphans.
- `_align_memory_to_ledger`: IDLE ghost detection — bots with `cycle_phase=IDLE` + zero ledger +
  zero physical now auto-reset regardless of `entry_confirmed=1`. Unblocks bots 10016, 10018.
- PASS 3 direction guard: `inject_adoption_row` blocked if `bot.direction != physical.side`.
  Prevented SHORT SUI being adopted into LONG SUI bot.
- `heal_cycle_fragmentation`: Only migrates NULL-cycle filled rows with CQB_ proof.
  Previously moved `status='new'/'open'` live orders across cycles, corrupting cycle accounting.

### v1.6.0 — 2026-04-10
- Proof-Only consensus enforced; all heuristic "gap patching" removed
- `safe_wipe_bot()` introduced as the sole authorized reset path
- Race-condition guard (`[RECON-RACE-GUARD]`) prevents double-fill on WS vs REST polling race

### v1.5.0 — 2026-04-07
- `cycle_phase` state machine introduced
- `CARRY_PENDING` guard prevents premature ghost-detection of carry positions
- DNA-HOLD guard added to `_align_memory_to_ledger` (later overridden by v1.7.0 IDLE check)

### v1.4.4 — 2026-03-20
- Gross-directional tracking replaces net math
- Multi-bot `reduceOnly` protection (sibling count check before flagging)

### v1.4.3 — 2026-03-15
- `ws_cache` symbol normalization unified
- `normalize_symbol()` extracted as shared utility

---

## 8. Testing Checklist

Run before every restart:
```powershell
python -m pytest tests/ -x -q --tb=short
# Expected: 64 passed, 6 skipped, 0 failed
```

Then verify in logs:
- [ ] No `[PHYS-ADOPT] Fatal` in engine.log
- [ ] `active_positions` row count matches Binance open positions count
- [ ] SUI SHORT shows correct qty in `active_positions`
- [ ] IDLE bots self-reset to Scanning after `_align_memory_to_ledger`
- [ ] No `SIZE DISCREPANCY` after 5+ cycles
- [ ] `[SYNC-DRIFT]` only fires when EE interval steps OR exchange holds wrong price — NOT every cycle
- [ ] `[SYNC-DRIFT]` log shows reason: `EE-stepped`, `price-drift`, or `qty-drift`
- [ ] No `python-dotenv could not parse` warnings in logs

---

## ⛔ RULE-ENV — The `.env` File Is Read-Only Except in Two Cases

```
THE .env FILE MUST NEVER BE WRITTEN BY ANY CODE EXCEPT ui/app.py's "Apply Settings" button.
IT MUST NEVER BE WRITTEN BY TESTS, SCRIPTS, OR DURING DEVELOPMENT.
```

### The only two authorised edits to `.env`

| When | What to change | How |
|------|---------------|-----|
| Rotating testnet credentials | Replace `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` | Edit manually in a text editor |
| Going live (future) | Add `BINANCE_API_KEY` and `BINANCE_API_SECRET` below the testnet keys | Edit manually in a text editor |

### What must NEVER touch `.env`

- **Tests** — all tests must mock `set_key` and `load_dotenv`. Never let pytest run code that calls `set_key()` against a real file path.
- **Scripts / debug tools** — no script in `engine/` or `tests/` may open and write `.env`.
- **The reconciler or engine** — they read `config.*` (loaded at startup). They never write back.

### Why this rule exists

A test that mocked `st.text_input()` but did NOT mock `set_key()` wrote 24 lines of `MagicMock` garbage to `.env`, causing `python-dotenv could not parse` warnings on every startup. The engine itself was unaffected (it reads `BINANCE_TESTNET_API_KEY`, not `BINANCE_API_KEY`), but the parse warnings are noisy and mask real issues.

### Defensive guard in `ui/app.py`

`ui/app.py` now validates values before writing:
- Must be a plain string (no `<` angle brackets — MagicMock repr starts with `<`)
- Must be ≥ 10 characters
- Must be printable ASCII only

If validation fails, the UI shows "❌ Invalid key format" and **nothing is written to disk**.

### Current `.env` canonical form

```
BINANCE_TESTNET_API_KEY=<your_key>
BINANCE_TESTNET_API_SECRET=<your_secret>
# BINANCE_API_KEY=       ← leave commented until mainnet
# BINANCE_API_SECRET=    ← leave commented until mainnet
TESTNET=True
DEMO_TRADING=True
DRY_RUN=False
LOG_LEVEL=INFO
ALLOWED_SYMBOLS=BTC/USDT,ETH/USDT,BNB/USDT,BTC/USDC
MAX_ORDER_USD=20000
GLOBAL_STOP_LOSS_PCT=70.0
```

If `.env` ever gains extra garbage lines (e.g. after a test run), restore this exact structure manually.

## 7. Reconciliation & Virtual Hedging

### One-Way Mode Virtual Hedging
- The system supports "Virtual Hedging" where multiple bots trade the same pair in One-Way mode.
- **Consensus Rule**: Individual bot drift is tolerated if the **Sum of System Ledgers** matches the **Exchange Net Position**.
- **Thresholds**: Alerts trigger at **$0.01 (1 cent)**. The system aims for absolute parity.

### Grid Placement Lockout (Safety Mechanism)
- **Lockout Rule**: If a bot has a pending reconciliation mismatch (`requires_manual_intervention = 1`), the Engine will **atomically block** grid placement.
- **Purpose**: This prevents the bot from "fighting" a mismatch or placing orders based on an unconfirmed average price.
- **Clearing Lockouts**:
    1. **Auto-Adoption**: If the bot is the sole bot for a pair and the direction matches, the system will auto-adopt the exchange state and clear the lockout.
    2. **Forensic Adopt**: Use the "Forensic Adopt" tool in the UI to scan history and force-sync the ledger.
    3. **Manual Reset**: Cycle the bot to Step 0 if the exchange is flattened.

### Dust-Aware Completion
- Bots will automatically transition to **Step 0 (Scanning)** if the residual notional value is **< $1.00 USD** after an Exit Order (TP).
- The residual "dust" is cleared from the database to prevent "Impossible Loop" deadlocks.

### 🛡️ Hedge-Aware Accounting & Cross-Cycle Parity (v2.5.3)
In Binance **One-Way Mode**, a 'hedge' order belonging to a LONG bot is executed as a SELL. This physically reduces or zeroes out the position on the exchange, while the bot remains virtually LONG in the system ledger.

To prevent the reconciliation engine from being "shocked" by this physical/virtual divergence, the system employs **Hedge-Aware Offsets**:
1.  **Reconciler**: The Math Capacity bound is extended by the sum of filled `hedge` minus `hedge_tp` orders. Crucially, this offset transcends `cycle_id` boundaries, as physical hedges survive TP cycles until explicitly closed.
2.  **Monitor**: The UI subtracts cross-cycle `hedge` and `hedge_tp` fills from the bot's virtual responsibility when comparing against physical net exposure.
3.  **Persistence**: The bot maintains its full gross martingale mass (`open_qty`) in the `trades` table, ensuring grid and TP logic continues to function while the position is locked.
4.  **Ghost Hedge Purge**: A strict rule exists in `database.py` (`_reset_bot_after_tp_internal`) — any destructive or manual wipe (`SYSTEM_WIPE`, `MANUAL_CLOSE`) forcibly clears all active hedges, ensuring dead positions do not mathematically corrupt the cross-cycle offset.
5.  **Proof-Only Consensus**: The reconciliation engine no longer performs algorithmic "guess" patches. If a physical position gap exists, the engine directly parses `fetch_my_trades` from the exchange. Only raw, cryptographically verified exchange footprints are translated into `adoption` offsets.
