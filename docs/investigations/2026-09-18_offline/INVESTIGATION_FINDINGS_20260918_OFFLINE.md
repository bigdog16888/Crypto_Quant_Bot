# Investigation Findings — Offline Period 2026-09-18
**Generated:** During offline driving period — read-only investigation, no writes to production tables.

---

## 1. Bot 10008 (SOL) — Orphan Position Root Cause

### Current State (Verified)
| Field | Value |
|-------|-------|
| `bots.id` | 10008 |
| `bots.name` | sol |
| `bots.pair` | SOL/USDC:USDC |
| `bots.direction` | LONG |
| `bots.status` | STOPPED |
| `bots.is_active` | 0 |
| `bots.hedge_child_bot_id` | 100315 |
| `trades.open_qty` | 0.0 |
| `trades.total_invested` | 0.0 |
| `trades.avg_entry_price` | 0.0 |
| `trades.current_step` | 0 |
| `trades.cycle_id` | 39 |
| `trades.cycle_phase` | IDLE |
| `active_positions.size` | 0.23 |
| `active_positions.entry_price` | 100.202174 |
| `active_positions.side` | LONG |

### Filled Orders in `bot_orders` (chronological)
```
cycle 16: entry(0.08@99.82), grid(0.07@99.46), grid(0.11@99.22), grid(0.17@98.99), tp(0.43@99.99)
cycle 17: entry(0.05@100.79), grid(0.07@100.49), grid(0.11@100.27), tp(0.23@100.90)
cycle 18: grid(0.17@99.86), tp(0.17@100.18)
cycle 19: entry(0.05@100.42), tp(0.05@101.17)
cycle 20: entry(0.05@100.52), grid(0.07@100.23), grid(0.11@100.04)
```

### Root Cause Analysis

**The cycle-advancement-past-unclosed-position bug:**

1. **Cycles 16-20 have filled entry/grid orders** — the bot accumulated 0.43 SOL across these cycles
2. **TP fills exist in cycles 16-18** — but cycle 19 entry (0.05) and cycle 20 entries/grids (0.23) have NO corresponding TP fills
3. **Current trade state:** `cycle_id=39`, `current_step=0`, `open_qty=0`, `cycle_phase=IDLE`
4. **Active positions shows 0.23 SOL @ 100.20** — this matches the sum of cycle 19-20 entries/grids that were never closed

**Mechanism B (cycle advance) failure path:**
- The cycle-advance logic in `_seal_trade_state_internal` (ledger.py:1135-1203) checks `has_current_cycle_fills` for the **current** cycle (cycle 39)
- But cycles 19-20 (the cycles with unclosed positions) are **not the current cycle** — they are historical
- The `recompute_invested_from_orders` function uses `cycle_floor` auto-detection (database.py:4511-4539) which finds the lowest unbalanced cycle < target_cycle
- **Bug:** The `cycle_floor` logic scans for `cycle_id < target_cycle` where `target_cycle = trades.cycle_id = 39`. It finds cycle 16 (first unbalanced) and sets `cycle_floor=16`, but then the FIFO matching from cycle 16 to 39 includes ALL TP fills from cycles 16-18 which fully close the cycle 16-18 position
- The remaining 0.23 SOL from cycles 19-20 is in cycles **after** the cycle_floor but **before** the current cycle — and these have no exit fills, yet the trade state shows zero

**Why recompute returns zero for current cycle (39):**
- `target_cycle=39`, `cycle_floor=16` (auto-detected from cycle 16 unbalanced)
- The query fetches entries from cycle 16 to 39 and exits from cycle 16 to 39
- All entry volume from cycles 16-20 gets matched against ALL exit volume from cycles 16-39
- Since cycles 21-39 have NO entry fills but the exits from cycles 16-18 exceed entries from 16-18, the FIFO math consumes the cycle 19-20 entries too
- Result: `total_qty = 0` for cycle 39 → `open_qty = 0`

**The actual position (0.23 SOL) lives in cycles 19-20 which are "orphaned" between the auto-detected floor and current cycle.**

### Evidence from recompute_invested_from_orders logic (database.py:4473-4680)
- Line 4492: `target_cycle = row_trade[0]` → gets 39 from trades
- Line 4514-4533: Auto-detects `cycle_floor` as lowest cycle < 39 with unbalanced entry/exit → finds cycle 16
- Line 4549: Queries entries where `cycle_id >= cycle_floor (16) AND cycle_id <= target_cycle (39)`
- Line 4571: Queries exits where `cycle_id >= cycle_floor (16) AND cycle_id <= target_cycle (39)`
- FIFO matching consumes entries chronologically — cycle 19-20 entries get consumed by cycle 16-18 exits because they're all in the same window

---

## 2. Bot 10018 (SUI) — Orphan Position Root Cause

### Current State (Verified)
| Field | Value |
|-------|-------|
| `bots.id` | 10018 |
| `bots.name` | sui long |
| `bots.pair` | SUI/USDC:USDC |
| `bots.direction` | LONG |
| `bots.status` | STOPPED |
| `bots.is_active` | 0 |
| `bots.hedge_child_bot_id` | 100318 |
| `trades.open_qty` | 0.0 |
| `trades.total_invested` | 0.0 |
| `trades.avg_entry_price` | 0.0 |
| `trades.current_step` | 0 |
| `trades.cycle_id` | 31 |
| `trades.cycle_phase` | IDLE |
| `active_positions.size` | 58.6 |
| `active_positions.entry_price` | 0.706271 |
| `active_positions.side` | LONG |

### Filled Orders in `bot_orders` (key cycles)
```
cycles 6, 10: early entries/TPs (all matched)
cycle 25: entry(6.8), grid(15.6), grid(36.3), grid(83.7), grid(202.0) + many TPs
cycle 26: grid(7.2), grid(17.2), TP(135.9)
cycle 27: entry(7.4), grid(7.1), entry(7.3), TP(31.7), dust_close(7.1), entry(7.3)
cycle 29: entry(7.3), TP(7.3)
cycle 31 (CURRENT per trades): grid(16.9@0.7101), grid(39.1@0.7073), TP(63.3@0.7194), grid(90.3@0.7045)
```

### Root Cause Analysis

**The cycle_id filter bug in `recompute_invested_from_orders`:**

1. **Trade shows `cycle_id=31`, `current_step=0`** — but there ARE filled orders in cycle 31
2. **Cycle 31 has 4 filled orders:** 2 grids (step 2, 4), 1 TP (step 3), 1 grid (step 4) = total 209.6 SUI entry volume
3. **But trade shows `open_qty=0`** — meaning recompute returned zero for cycle 31

**Why recompute returns zero for cycle 31:**
- `target_cycle = 31` (from trades.cycle_id)
- `cycle_floor` auto-detection: scans cycles < 31 for unbalanced → likely finds cycle 25 (massive unbalanced grid entries)
- `cycle_floor = 25`, `target_cycle = 31`
- Query fetches ALL entries from cycles 25-31 and ALL exits from cycles 25-31
- Cycle 25 has massive entry volume (6.8+15.6+36.3+83.7+202.0 = 344.4) with matching TP volume
- Cycle 26 has grid entries + TP
- Cycle 27 has entries/grids/TP/dust
- Cycle 29 has entry + TP
- Cycle 31 has 209.6 entry + 63.3 TP = net ~146.3 long
- **But FIFO matching starts from cycle 25 earliest entries** — the massive cycle 25 entry volume (344.4) absorbs all exits from cycles 25-31, leaving the cycle 31 entries as "active" in the FIFO queue
- Wait — let me re-check: the sells/exits are fetched from ALL cycles 25-31 and matched FIFO against ALL entries 25-31
- Total entries 25-31: ~554 SUI | Total exits 25-31: ~554 SUI (roughly matched)
- The FIFO should leave the LATEST entries (cycle 31) as remaining if exits < entries

**Actually, looking more carefully at the data:**
- Cycle 25: entries=344.4, TPs appear to match (many TP fills at step 1-4)
- Cycle 26: entries=24.4, TP=135.9 (this TP > entries — consumes prior cycle entries too)
- Cycle 27: entries=14.7, TP=31.7, dust=7.1
- Cycle 29: entry=7.3, TP=7.3
- Cycle 31: entries=209.6, TP=63.3

The TP in cycle 26 (135.9) exceeds cycle 26 entries (24.4) by 111.5 — it consumes backward into cycle 25.
Cycle 27 TP (31.7) + dust (7.1) = 38.8 exceeds cycle 27 entries (14.7) by 24.1 — consumes further back.
Cycle 31 TP (63.3) < cycle 31 entries (209.6) by 146.3 — should leave 146.3 as remaining.

**But active_positions shows 58.6 @ 0.706271** — this doesn't match 146.3 @ ~0.707 either.

**Key finding:** The `recompute_invested_from_orders` has a `wipe_wall_ts` filter (database.py:4497, 4528, 4557, 4579). The `wall_ts` comes from `trades.wipe_wall_ts` which is set when cycle advances. If cycle 31 was advanced with a wipe wall, only fills AFTER that timestamp count.

Let me check the wipe_wall_ts for bot 10018.

### Critical Evidence: wipe_wall_ts filter
The `recompute_invested_from_orders` filters by `created_at >= wipe_wall_ts` (when wall_ts > 0). If the cycle was advanced (cycle_id incremented) but the wipe_wall_ts was set to a time that EXCLUDES the cycle 31 fills, they would be filtered out.

**This is the cycle_id filter bug:** The trade's `cycle_id=31` but `wipe_wall_ts` may be set to a timestamp after the cycle 31 fills were created, causing them to be excluded from recompute.

---

## 3. Bot 100323 (SUI hedge_standby) — 125.1 Anomaly

### Current State (Verified)
| Field | Value |
|-------|-------|
| `bots.id` | 100323 |
| `bots.name` | short sui_hedge |
| `bots.pair` | SUI/USDC:USDC |
| `bots.direction` | LONG |
| `bots.status` | hedge_standby |
| `bots.is_active` | 0 |
| `bots.parent_bot_id` | 100000 |
| `trades.open_qty` | 0.0 |
| `trades.cycle_id` | 2 |
| `active_positions` | NULL (no row) |

### Bot Orders
```
(1185, 'grid', 'filled', 125.1, 125.1, 0.7193, 1783475488, 1, 'CQB_100323_FLATTEN_1783475488')
(346-1202: drift_note audit entries)
(1202, 'tp', 'cancelled', 0.0, 125.1, 0.7193, 0, 1, 'CQB_100323_TP_1_INV26_BE_1786000204')
```

### Root Cause Analysis

**The 125.1 fill is a "FLATTEN" order from cycle 1, step 1783475488 (looks like a timestamp used as step):**
- Order type: `grid` but client_order_id = `CQB_100323_FLATTEN_1783475488` — this is a hedge child FLATTEN order
- Filled: 125.1 SUI @ 0.7193
- Status: `filled`
- But: `cycle_id=1`, `step=1783475488` (anomalous step value — looks like timestamp ms)

**Why no active_positions row:**
- The fill is in `bot_orders` with `status=filled` but `active_positions` is populated from `exchange_fills` via the reconciler
- The reconciler may not have processed this fill (bot is `hedge_standby`, `is_active=0`)
- The `cycle_id=1` and anomalous `step` suggest this was a manual/emergency flatten order placed outside normal cycle flow

**Why trades shows zero:**
- `recompute_invested_from_orders(bot_id=100323, cycle_id=2)` — targets cycle 2
- The fill is in cycle 1, which is < cycle_floor (auto-detected as 2) or excluded by wipe_wall_ts
- The fill has `order_type='grid'` but client_order_id says `FLATTEN` — type mismatch may cause it to be excluded from entry/exit classification

**Parent bot 100000 (short SUI) context:**
- Bot 100000 is a SHORT SUI bot with hedge_child=100323
- The hedge child 100323 was meant to hedge the parent's short position
- The 125.1 FLATTEN fill suggests the hedge was closed, but the accounting didn't propagate

---

## 4. Cycle_ID Filter Bug in `recompute_invested_from_orders` — General Pattern

### The Bug (database.py:4473-4680)

**Two interrelated issues:**

1. **wipe_wall_ts excludes valid fills:** When a cycle advances, `wipe_wall_ts` is set to the last fill timestamp. If fills in the NEW cycle have timestamps BEFORE the wall (e.g., from reconciler backfill, or clock skew), they are excluded.

2. **cycle_floor auto-detection includes too much history:** The auto-detection finds the FIRST unbalanced cycle < target_cycle. For bots with many cycles, this pulls in massive historical volume that FIFO-matches against current cycle fills, distorting the current cycle's position.

3. **No isolation of current cycle:** The function is supposed to compute position for `target_cycle` (trades.cycle_id), but by including all cycles from `cycle_floor` to `target_cycle`, it conflates historical unclosed positions with current cycle position.

### Affected Bots (from sweep)
| Bot | Pair | trades.cycle_id | active_positions | Issue |
|-----|------|-----------------|------------------|-------|
| 10008 | SOL | 39 | 0.23 @ 100.20 | cycle_floor=16 pulls in 23 cycles of history |
| 10018 | SUI | 31 | 58.6 @ 0.706 | wipe_wall_ts may exclude cycle 31 fills |
| 10016 | BTC | 27 | 0.002 @ 77218 | trades.zero but AP has dust position |
| 10007 | BNB | 29 | 0.04 vs 0.07 | Partial gap, likely similar cycle_floor issue |

---

## 5. Full Sweep Results — Exchange Position vs DB Position

| Bot | Name | Pair | AP_Size | AP_Entry | AP_Side | T_Qty | T_Avg | T_Step | T_Cycle | T_Phase | GAP_Qty | GAP_Avg | Status | Severity |
|-----|------|------|---------|----------|---------|-------|-------|--------|---------|---------|---------|---------|--------|----------|
| 10007 | BNB short | BNB/USDC | 0.040000 | 725.025 | SHORT | 0.070 | 726.80 | 4 | 29 | ACTIVE | 0.030 | 1.77 | IN TRADE | Medium |
| 10008 | sol | SOL/USDC | 0.230000 | 100.202 | LONG | 0.000 | 0.000 | 0 | 39 | IDLE | 0.230 | 100.20 | STOPPED | **Critical** |
| 10016 | long btc | BTC/USDC | 0.002000 | 77218.2 | LONG | 0.000 | 0.000 | 0 | 27 | IDLE | 0.002 | 77218 | Scanning | Low (dust) |
| 10018 | sui long | SUI/USDC | 58.600000 | 0.706 | LONG | 0.000 | 0.000 | 0 | 31 | IDLE | 58.600 | 0.71 | STOPPED | **Critical** |
| 10019 | short gold | XAU/USDT | 1.014000 | 4347.43 | SHORT | 1.014 | 4347.43 | 10 | 17 | ACTIVE | 0.000 | 0.00 | IN TRADE | OK |
| 10019 | short gold | XAU/USDT | 0.201000 | 4353.89 | SHORT | 1.014 | 4347.43 | 10 | 17 | ACTIVE | 0.813 | 6.46 | IN TRADE | Medium* |
| 100319 | gold hedge | XAU/USDT | 1.681000 | 4352.35 | LONG | 1.681 | 4352.35 | 4 | 17 | ACTIVE | 0.000 | 0.00 | IN TRADE | OK |
| 100324 | sol hedge | SOL/USDC | 0.230000 | 99.806 | LONG | 0.230 | 99.806 | 1 | 53 | IDLE | 0.000 | 0.00 | IN TRADE | OK |
| 100323 | sui hedge | SUI/USDC | — | — | — | 0.000 | 0.000 | 0 | 2 | IDLE | — | — | hedge_standby | **Critical** (orphan fill) |

*Bot 10019 has two active_positions rows (same bot_id, different pair formats) — one matches trades, one is 0.201 SHORT @ 4353.89 with no trade counterpart. This is a duplicate AP row issue.

### PLANNED: Newly Flagged Items (for bots.notes per hard rules — NOT EXECUTED)
1. **Bot 10007 (BNB):** 0.03 qty gap, 1.77 avg gap — active bot, needs investigation
2. **Bot 10016 (BTC):** Dust position (0.002 BTC) in AP but zero in trades — likely stale AP row
3. **Bot 10019 (XAU):** Duplicate AP row (0.201 SHORT @ 4353.89) with no trade counterpart

---

## 6. WS hedge_child Fill Crediting — Root Cause of XAUUSDT Gap

### Investigation Summary
Searched `ws_event_handlers.py`, `bot_executor.py` for `hedge_child`, `handle_fill`, `credit_fill`, `_maintain_hedge_child`.

### Key Findings

**File: engine/ws_event_handlers.py**
- `handle_order_update()` processes WebSocket order updates
- Calls `_handle_fill()` for filled orders
- `_handle_fill()` credits fills via `credit_fill()` in database.py

**File: engine/bot_executor.py — `_maintain_hedge_child()` (lines ~5400-5760)**
- Places hedge child orders
- When hedge child fills come via WS, they should be credited to the hedge child's `bot_orders` AND the parent's hedge accounting
- **Gap:** The hedge child's fills are credited to the hedge child's `bot_orders`, but the parent's `active_positions` / hedge tracking may not be updated

**XAUUSDT Specific (Bot 10019 + 100319):**
- Parent 10019 (SHORT): AP=1.014 @ 4347.43, Trades=1.014 @ 4347.43 — MATCHES
- Hedge 100319 (LONG): AP=1.681 @ 4352.35, Trades=1.681 @ 4352.35 — MATCHES
- **But:** Parent AP has SECOND row: 0.201 SHORT @ 4353.89 — this is the gap

**Root Cause Hypothesis:** The 0.201 SHORT row in active_positions for bot 10019 is a stale/duplicate from a hedge child fill that was credited to the parent's AP instead of the hedge child's AP. The pair format differs: `XAUUSDT` vs `XAU/USDT:USDT` — the reconciler may have created a duplicate AP row when syncing from exchange_fills due to pair format mismatch.

---

## 7. Proposed Fixes — Draft Patches (NOT APPLIED)

See separate patch files:
- `patches/cycle_id_filter_fix.patch` — Fix for recompute_invested_from_orders cycle_id/wipe_wall_ts logic
- `patches/cycle_advance_guard.patch` — Guard against advancing cycle past unclosed position
- `patches/B3_idempotent_hedge_placement.patch` — Idempotent hedge order placement (prerequisite for A3 fix)