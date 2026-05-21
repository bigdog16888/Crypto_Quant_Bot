# Operator Runbook — Pair Mismatch & MANUAL GATE (v3.5.2)

**Read when:** Global Netting shows `SYSTEM MISMATCH`, bots show `🚨 MANUAL GATE`, or Order Health says `MISSING CRITICAL ORDERS`.

**Architecture:** `docs/ARCHITECTURE_v3.5.md` | **Changelog:** `docs/CHANGELOG.md`

### Pass/fail rule (v3.5)

| UI | Meaning |
|----|---------|
| **HEALTHY** | `audit_pair_ledger_vs_exchange()` = **0** mismatched pairs (qty within `PAIR_PARITY_QTY_TOLERANCE`, default 0.002) |
| **MISMATCH** | One or more pairs outside tolerance — trading on those pairs is gated until repair or proof flatten succeeds |

Dollar columns in the UI are illustrative (`qty × price`). **Decisions are made in contract qty space.**

---

## Your current situation (2026-05-19 logs)

| Pair | Ledger | Exchange | Root cause | Auto-fix |
|------|--------|----------|------------|----------|
| **XRP** | +292.1 | **0** | Phantom ledger (`entry`/`grid`/`adoption` rows, position already closed on Binance) | **Yes** — `TESTNET_PURGE_PHANTOM_LEDGER` safe-wipes bots |
| **SUI** | −27.1 | −7.5 | `short sui` cycle 33 has 27.1 units booked; exchange only −7.5 (fills closed on exchange, not credited as `tp` in DB) | **No** — needs proof flatten or trade-history `tp` credit |
| LINK/SOL | (was red) | — | Often fixed after startup repair / purge | — |

`MISSING CRITICAL ORDERS: short sui` = parity gate blocking orders until SUI is fixed (correct).

Engine log markers: `[STARTUP-PARITY]`, `[STARTUP-PAIR-REPAIR]`, `[PHANTOM-PURGE]`, `[PROOF-FAILED]`.

---

## What v3.5 does (fundamental, not cosmetic)

| Before v3.5 | After v3.5 |
|-------------|------------|
| Startup `[HEALING]` used raw SQL on `filled_amount` | **Only** `credit_fill` + pair heal budget |
| Parity gate blocked TP/grid on in-trade bots | **`gate_maintain_orders_allowed`** — grids restore while ledger repair runs |
| DB “grid exists” blocked placement with no exchange order | Stale row **cancelled**, grid re-placed |
| Ledger > exchange after bad heal | **`deflate_pair_ledger_overcount`** on startup |
| Ledger 0, exchange has size (XAU class) | **`repair_exchange_orphan_when_ledger_flat`** (CQB proof or flatten) |
| Stop Monitoring hung 30s+ | **`shutdown_control`** — lock released first |

v3.4 stopped **new** corruption; v3.5 **enforces** proof paths and **deterministic repair** when startup audit fails.

---

## What to run (always)

Your normal flow is valid:

| Step | What it does |
|------|----------------|
| `run_bot.bat` | Opens **Streamlit UI only** |
| **Pre-Flight Sync** (Monitor) | Quick REST snapshot → `active_positions` (UI helper) |
| **▶️ Start Monitoring** (sidebar) | Starts `engine/runner.py` — **this is the real bot** |

`Start Monitoring` = `restart_runner.bat` / `python engine\runner.py`.  
On start, `startup_sync` runs: inflate heal, dedup, offline CQB fills, parity audit, per-pair CQB trade-history repair.

**One-click alternative:** `run_stack.bat` (engine + UI).

Optional:
```bat
python scripts\run_startup_heal.py
```

---

## Should I click "Start Monitor"?

| Situation | Start Monitor? |
|-----------|----------------|
| Any row in **Global Netting** still red (LINK, SOL, SUI, XRP, …) | **No** for autonomous trading on those pairs — bots are `MANUAL GATE` and engine blocks orders anyway |
| Only green pairs (e.g. BTC, ETH, BNB matched) and engine running | **Yes** — OK for matched pairs |
| You want dashboard visibility only | UI is fine; engine must still run for fills/orders |

`MISSING CRITICAL ORDERS: short link, short sui` is **expected** while those bots are in `MANUAL GATE` — the system is **refusing** to place TP/grid until parity is fixed. Do not treat this as a bug to override.

---

## The four mismatches — what to do

Use **Global Netting** qty columns (`sys` vs `ex`), not dollar diff alone.

### Pattern A — Ledger **smaller** than exchange (ghost exchange size)

**Your pairs:** LINK (`sys=-0.54`, `ex=-1.08`), SOL (`sys=-1.32`, `ex=-4.64`)

**Meaning:** DB thinks less short than Binance holds (often old cycle reset while exchange did not flat).

**Recommended action:** **Proof flatten** — Monitor → that pair’s mismatch row → `💥 Close` → confirm.

- Closes **entire pair net** on Binance (one-way: one position per symbol).
- Resets **all active bots** on that symbol after exchange is verified flat.
- Safe “clean slate” for the symbol.

### Pattern B — Ledger **larger** than exchange (phantom ledger)

**Your pairs:** SUI (`sys=-27.1`, `ex=-7.5`), XRP (`sys=+292.1`, `ex=+157.9`)

**Meaning:** DB over-counts fills (missing TP credits, bad adoption history, or duplicate rows).

**Options (pick one):**

1. **Preferred if you want to keep the live position:**  
   - Do **not** click Close yet.  
   - Run `python scripts\run_startup_heal.py` with engine API keys.  
   - Reconcile from Binance trade history (support path: credit missing `tp`/`close` rows via proof IDs).  
   - Goal: shrink **virtual** to match **exchange** without nuking the trade.

2. **Nuclear clean slate (you accept closing the real position):**  
   - `💥 Close` on that pair — flattens **exchange** to 0, then resets all bots on that pair.  
   - Use when you want to stop trading that symbol and restart from zero.

**Warning on XRP:** Exchange still has a **large long** (~158 units). Close will **market-sell** that entire net position. Only use if you intend to flat XRP on the account.

---

## Step-by-step (recommended order)

```
1. restart_runner.bat
2. python scripts\run_startup_heal.py
3. Refresh Monitor → Global Netting
4. For each RED pair:
     Pattern A (|sys| < |ex|)  → 💥 Close on that row
     Pattern B (|sys| > |ex|)  → heal/history first OR 💥 Close if you want flat
5. Confirm row green (sys ≈ ex)
6. Bots leave MANUAL GATE → can show TP/GRID again
7. Start Monitor (if engine already running)
```

---

## What NOT to do

- Do not manually trade on Binance to “fix” ledger routinely.
- Do not DB-delete `bot_orders` or wipe bots without proof flatten.
- Do not set `ALLOW_FORENSIC_ADOPT=True` unless you understand it can re-inflate the ledger.
- Do not ignore XRP size — largest notional risk in your list.

---

## Revert v3.4.0 if behavior is wrong

**If you use git** (after you commit this work):

```bat
git log --oneline -5
git revert <commit-hash-of-v3.4.0> --no-edit
```

Or restore files from the commit before Phase A:

- `engine/parity_gates.py` (delete)
- `engine/database.py`, `bot_executor.py`, `ws_event_handlers.py`, `reconciler.py`, `ledger.py`, `ui/views/monitor.py`, `config/settings.py`

**Soft disable without revert** (emergency only — brings back old failure modes):

```env
ALLOW_FORENSIC_ADOPT=True
PAIR_PARITY_QTY_TOLERANCE=999999
```

Then restart engine. Prefer git revert for a clean rollback.

---

## Commit message template (when you commit)

```
feat(v3.4.0): Phase A pair parity gates and proof-only flatten

- Block cycle reset when exchange net != projected pair virtual
- Block entry/maintain on mismatched pairs (MANUAL GATE)
- Disable forensic WS adopt by default
- UI mismatch Close uses proof_flatten_pair
- docs: OPERATOR_MISMATCH_RUNBOOK.md, CODEBASE_GUIDE §3.19
```

---

## Quick reference — patterns

| Pattern | Meaning | Action |
|---------|---------|--------|
| A: `|ledger| < |exchange|` | Exchange holds more than books | Proof flatten or CQB fill scan |
| B: `|ledger| > |exchange|` | Books over-count (missing `tp`) | Trade-history repair, or proof flatten |
| **C: exchange ≈ 0, ledger large** | Phantom ledger (XRP) | `python scripts\repair_phantom_ledger.py` or restart engine (testnet auto-purge) |

## Professional workflow (target state)

```
run_bot.bat → Start Monitoring
    → startup_sync (prime, CQB fills, parity audit, pair repair, phantom purge)
    → only green pairs trade; red pairs = MANUAL GATE
    → fix red pair once (purge / flatten / history proof)
    → parity green → bot returns to SCANNING / IN TRADE
```

**Scripts (supported):**

| Script | Use |
|--------|-----|
| `scripts/run_startup_heal.py` | Inflate cap, dedup, verify fills, parity flag |
| `scripts/repair_phantom_ledger.py` | Wipe ledger when exchange net = 0 (XRP class) |

**Env:**

```env
TESTNET_PURGE_PHANTOM_LEDGER=True   # default on testnet
ALLOW_FORENSIC_ADOPT=False
PAIR_PARITY_QTY_TOLERANCE=0.002
```

Matched bots (long btc, long eth, short btc) can run while engine is up and their pair is green in Global Netting.

---

## Healthy system — what to watch (monitoring period)

When Global Netting shows **0 mismatched pairs** and **ORDERS SYNCED**:

| UI area | What it means |
|---------|----------------|
| **Total Invested** (header) | Sum of `trades.total_invested` — ledger exposure in USD |
| **In Trade / Scanning** | How many bots have open baskets vs idle |
| **Open Qty (Notional)** | `open_qty × avg_entry` across bots |
| **Active Bot Positions** table | Per-bot Total Invested, Open Qty, Avg Entry, TP/Grid |
| **LAST: MANUAL_CLOSE** | Normal after you closed SUI/XRP/etc. manually |

**Prove the fix (not a patch):** After each TP hit, check Global Netting stays green without manual Close. If red appears, check `engine.log` for `[CYCLE-RESET-BLOCKED]` or `[PAIR-LEDGER-MISMATCH]`.

Full architecture: `docs/ARCHITECTURE_v3.5.md`

---

## Known patterns — v3.5.2 additions

### Pattern D — `WIPE BLOCKED` loop after startup orphan flatten (v3.5.2 fixed)

**Log signature:**
```
[ORPHAN-EXCHANGE] XRP/USDC:USDC: ledger≈0, exchange=16.9 — flattening exchange...
[ORPHAN-EXCHANGE] Bot 10017 reset after flatten: WIPE BLOCKED: Bot 10017 has live LONG position (16.90)
[ORPHAN-EXCHANGE] XRP/USDC:USDC: ledger flat, exchange had 16.9 — flattened 16.9.
Raw API Error 400: {"code":-2022,"msg":"ReduceOnly Order is rejected."}
```

**What happened:** Startup orphan repair flattened exchange correctly, but the bot ledger was never
cleared — `active_positions` still showed the pre-flatten size. The engine then tried to TP-maintain
a phantom position every 7 s, each attempt getting `-2022` rejected.

**Status in v3.5.2:** Auto-fixed. `repair_exchange_orphan_when_ledger_flat` now deletes the stale
`active_positions` row before resetting. If you see this pattern on an older build, restart the
engine (v3.5.2 startup repair will resolve it) or run `proof_flatten_pair` via the UI `💥 Close`.

**Manual check (confirm system is clean after startup):**
```sql
SELECT b.name, b.pair, t.open_qty, t.total_invested, t.cycle_phase, b.status
FROM bots b JOIN trades t ON t.bot_id = b.id
WHERE b.is_active = 1
AND (t.open_qty > 0 OR t.total_invested > 0)
ORDER BY b.pair;
```
If this returns **only genuinely in-trade bots** (those with real exchange positions), and Global
Netting shows **0 mismatches**, the system is clean.
