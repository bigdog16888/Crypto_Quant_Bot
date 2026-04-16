# Crypto Quant Bot — AI Agent Codebase Guide
**Version: 1.8.1 | Last Updated: 2026-04-16**

> **READ THIS FIRST** before touching any code. This is the single authoritative guide.
> It supersedes `UNIFIED_BOT_DOCUMENTATION.md` and all older session notes.
> Every invariant here was added because someone violated it and the system broke.

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

### 3.9. Ledger Mathematics — Canonical Form
All position calculations MUST use:
- **Entries** (add to position): `order_type IN ('entry', 'grid', 'adoption_add', 'adoption')`
- **Exits** (subtract from position): `order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl')`
Any deviation creates ghost balances or zero-out errors.

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

### 3.12. Early Exit (EE) Decay Baseline
The EE formula decays the Take-Profit parameter incrementally across time towards the Average Entry. `basket_start_time` manages this timing sequence. It MUST be natively updated to `int(time.time())` in `accumulate_trade_fill` asynchronously upon ANY limit order hitting the ledger (both Entries AND Grid level averages). Doing this correctly scales profitability targets rather than abruptly crashing freshly loaded grid margins into Break-Even status because of old entry cycle footprints.

### 3.12. Carry-Over Ghost Mass Protection
Administrative exits (`SYSTEM_WIPE`, `MANUAL_CLOSE`, `STOP_LOSS_EXIT`) must NEVER trigger carry-over.
`reset_bot_after_tp` uses `action_label` to detect admin exits and skip carry propagation.

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

### v1.7.0 — 2026-04-14
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

Before restarting after any code change:
- [ ] `python -c "import py_compile; py_compile.compile('engine/reconciler.py', doraise=True); py_compile.compile('engine/database.py', doraise=True)"` — syntax clean
- [ ] `active_positions` row count matches Binance open positions count after startup
- [ ] No `[PHYS-ADOPT] Fatal` in engine.log (was crashing due to f_side NameError)
- [ ] SUI SHORT shows correct qty (168.2, not 84.1) in `active_positions`
- [ ] IDLE bots (10016 BTC, 10018 SUI) self-reset to Scanning after `_align_memory_to_ledger`
- [ ] No `SIZE DISCREPANCY` after 5+ cycles
- [ ] BNB short `[RECOMPUTE-CARRY]` shows -0.04 BNB (from bot_orders), not -0.05 (from trades fallback)
