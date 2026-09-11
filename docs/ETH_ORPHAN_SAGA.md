# ETH Orphan Saga — full story, 2026-08-28 → 2026-09-04

> One place that tells the whole ETH-orphan story cleanly: how it opened,
> how it froze the pair, how it was root-caused, how it was closed, how the
> DB was healed, and what the state is now. Written 2026-09-04 after final
> verification (engine cycling, all gates green).

## TL;DR

The 0.901 ETH orphan (exchange-held LONG, no ledger owner) was closed
**2026-09-04 14:30:08** at **2504.26** via reduceOnly MARKET SELL
(order `1012525341`), realizing **+$88.58 gross / +$87.56 net** and lifting
DB-reported equity onto the same footing as the exchange. The stale
`active_positions` row was healed (single-row DELETE), the engine was
restarted from **0531b2c**, and on the first seal pass all 6 ETH bots
self-cleared REQUIRE_MANUAL_PROOF to Scanning/hedge_standby. The 6 ETH bots
(+2 LINK bots) remain excluded from trading via STARTUP_EXCLUDED_BOT_IDS —
they resume only on operator decision.

## Timeline

### Origin (2026-08-28, the accident)

- The engine's catchup-entry path placed ETH entries (`CQB_100316_ENTRY_4_9`,
  `CQB_100316_ENTRY_4_1_CATCHUP_*`) that filled on the exchange **after** the
  engine had already reset their `bot_orders` rows to `reset_cleared` /
  `auto_closed` — the "catchup-fill-credit race" (see
  `references/catchup-fill-credit-race-2026-08-28.md`).
- Result: 0.904 ETH LONG physical on the exchange with **no trades row owning
  it** — a ledger orphan. Entry lineage: bot 10011 TP order
  `CQB_10011_TP_4_10_R1788338122` filled 0.904 @ 2405.95 (Sep 2),
  dust-close leftovers, and the hedge child 100316's FLATTEN of 3.07 ETH
  (@2406, order `1011451852`, the freeze-scope-gap incident's autonomous
  flatten) netted down to 0.901 residual.
- The orphan left the DB under-reporting true equity by ~$2,267
  (2026-09-03: true 17,112.21 vs DB 14,845.53 — parked accounting note).

### Freeze (the containment)

- Startup barrier + GTR flagged ETHUSDC as `PAIR_ORPHAN_PHYSICAL`
  (virtual flat, physical 0.901). Reconciler could not adopt it (no CQB
  fill to attribute — Demo FAPI WS fills have empty clientOrderId for
  attribution purposes).
- All 6 ETH bots (10011, 10021, 100002, 100316, 100321, 100325) were set to
  `REQUIRE_MANUAL_PROOF` and added to `STARTUP_EXCLUDED_BOT_IDS`
  (config/settings.py:122, together with LINK 10020/100320). FREEZE-GUARD
  (post-2026-09-02 hardening) enforced this at every runtime order path.
- The pair sat frozen for a week while the operator decided what to do.

### Root cause of the persistence (why it couldn't self-heal)

- The orphan was a **position with no ledger trail**: no CQB-tagged fill
  existed to adopt (the trades that made it were terminal-statused away
  before credit). Forensic Adopt (`perform_forensic_reconstruction`) scans
  for CQB-tagged fills — a scan can't find what never existed.
- The DB's `active_positions` row (bot 10021, LONG 0.901 @ 2405.95) was a
  **virtual remnant**: stale by 8+ hours at close time, never refreshed
  because the pair was frozen.
- The UI's "Close Orphan" button (ui/views/monitor.py:898-928) is
  fire-and-forget: places the reduceOnly close from a stale UI frame, never
  waits for fill, never heals the DB. Deemed unsafe (2026-09-04) — the
  operator's Option A (close → verify → heal) was executed instead.

### Close (2026-09-04)

- **14:16:27** — engine self-killed via DEPLOY-OUTDATED (freshness guard):
  the prior session wrote `scripts/snap_eth_orphan_preexecute.py` under the
  running engine. Guard fired correctly; clean shutdown, SocketLock
  released, last_shutdown.ts written 14:16:30.
- **14:17:52** — pre-execute snapshot saved to repo root
  (`ETH_ORPHAN_PRE_EXECUTE_SNAPSHOT_20260904.json`): all 6 bot rows, 30
  recent ETH orders, whitelists, active_positions.
- **14:19** — `scripts/close_eth_orphan.py` written (dry-run default,
  `--execute` places a reduceOnly MARKET SELL via the engine's own
  `ExchangeInterface.create_order` wrapper, polls `fetch_order` to
  `closed`, verifies exchange-flat, no DB writes).
- **14:30:07** — order placed (clientOrderId `CQB_ORPH_ETHUSDC_788503407067`),
  **filled 14:30:08.994** — trade 108108218: SELL 0.901 @ 2504.26,
  cost 2256.33826, commission 1.01535221 (taker 0.045%).
- Operator closed the session window mid-flow; the fill report never
  rendered. The close itself was complete on the exchange.
- A follow-up session (this one) re-verified from exchange records:
  order status=filled, filled=0.901, average=2504.26; wallet delta
  +87.56 matches price-arithmetic net PnL to the cent.

**Realized PnL (exchange-sourced):** gross (2504.26 − 2405.95) × 0.901 =
**88.577310**; net of the 1.01535221 close commission = **87.561958 USDC**.
DB equity at the next Circuit Check (15:48) rose to $9,121.26 — matching the
wallet — the $2,267 accounting gap is gone.

### Heal (2026-09-04, this session — two-gate)

- Gate 1 (operator): approved minimal heal scope — remove the stale
  `active_positions` ETH row only; no bot-status writes; let the engine's
  own seal logic clear REQUIRE_MANUAL_PROOF; keep the 6 bots in the
  exclusion list (trading resume is a separate, later decision).
- Pre-heal snapshot: `ETH_ORPHAN_PRE_HEAL_SNAPSHOT_20260904.json` (repo root).
- First `--execute` attempt failed safe (SQL param-order swap,
  rowcount=0, assertion aborted, zero rows touched). Fixed the tuple
  order; second attempt: rowcount=1, ETH rows 0, total 2→1, 6 bots
  untouched, whitelists 0. `scripts/eth_orphan_heal.py` is the tool.
- **Restart from 0531b2c** at 15:39:18 (SocketLock PID 8592). PREFLIGHT
  3/3 PASS with `ETHUSDC: Net Parity (Exchange=$0.00 | DB=$-0.00)` on the
  first barrier pass — the heal + prior close made parity trivially true.
- Startup barrier step 2/8 found the engine-down window (~2.4h) had left a
  SOL entry fill un-credited (placed 14:15:33, filled 15:08:10 while
  dead): PRE-COMMIT-RESOLVE restored it, SNAP-ALLOCATE assigned SOL −0.09
  to bot 100001, PLAUSIBILITY-OK db=−0.09/exchange=−0.09 gap=0, TP live.
  The 3 PLAUSIBILITY-BLOCK lines for 10008/100315/100324 are per-bot
  warnings on flat siblings — pair-level consensus confirmed
  (`VIRTUAL-CONSENSUS: 4 bots net to -0.090000`).

### Current state (post-heal, verified 2026-09-04 ~15:53)

| Check | Result | Evidence |
|---|---|--- saga-state
| Engine | UP from 0531b2c, PID 8592, cycling ~12s | port 19888 LISTENING; Circuit Checks 15:52:54, 15:53:03, 15:53:10 |
| ETH parity | 0/0 | preflight `ETHUSDC: Net Parity (Exchange=$0.00 \| DB=$-0.00)` |
| RMP status | 6/6 cleared | seal lines 15:39:42–43: `REQUIRE_MANUAL_PROOF cleared — position is now flat after TP` ×6 |
| GTR orphan list | empty | 15:47:19 + 15:52:21 passes: `orphan=[] manual_proof=[] in_sync=2` |
| Trading exclusion | intact | FREEZE-GUARD 15:40:12: ETH 6 bots + LINK 2 bots `skipping resolve_net_mismatch repair actions` |
| SOL | cycling, TP live | `SOLUSDC BUY NEW #403007376` 15:40:19; SNAP-ALLOCATE clean; 100001 IN TRADE, 10008/315/324 flat siblings |
| BNB | cycling | SNAP-ALLOCATE 15:48:17 BNBUSDC Net matches (−0.02); 10007 maintain_orders |
| XAU/gold | cycling, flat, entry pending CCI trigger | `short gold: Triggers: CCI=✅ → Entry=YES` 15:48:18; DEDUP-GUARD on stale CID |
| Equity | $9,121.27 (wallet + uPnL, O-3-hardened Circuit Check) | 15:53:10 Circuit Check |

## Fee model (measured on this venue — Binance Demo FAPI)

- **Maker 0.0180%** — TP limit buys (GTX postOnly), tradeIds 107418582–85.
- **Taker 0.0450%** — every MARKET order: DUST sells, FLATTEN sells, and the
  orphan close (108108218). Confirmed by commission/notional on 8 fills.
- USDC pairs are NOT zero-fee (maker still pays 0.018%). USD1 pairs exist
  (BTCUSD1, ETHUSD1, TRADING) but their fee schedule is **unverified** —
  needs its own probe before trading (do not assume it mirrors USDC).
- `fetch_trading_fees`/`fetch_markets`/`fapiPrivateGetAccount` don't work
  on Demo FAPI (ccxt incompatibility) — fee evidence must come from actual
  fill commissions (income records), not schedule metadata.

## Artifacts (audit trail)

- `ETH_ORPHAN_PRE_EXECUTE_SNAPSHOT_20260904.json` (repo root) — pre-close state
- `ETH_ORPHAN_PRE_HEAL_SNAPSHOT_20260904.json` (repo root) — pre-heal state
- `scripts/close_eth_orphan.py` — the close tool (kept for audit)
- `scripts/eth_orphan_heal.py` — the heal tool (kept for audit)
- `scripts/eth_fill_verify.py` — the fill/PnL verifier (kept for audit)
- `scripts/eth_fee_model_check.py` — fee-model evidence collector (kept)
- `engine_restart_20260904.log` — full restart log, first barrier pass included

## What's still open (honestly)

1. **ETH/LINK trading resumption** — operator decision, not automated. The 6
   ETH bots are RMP-cleared but stay in `STARTUP_EXCLUDED_BOT_IDS` (with
   LINK 10020/100320) until removed by operator.
2. **USD1 fee verification** — before any USD1-pair trading.
3. **UI "Close Orphan" button** — still fire-and-forget with stale-frame
   qty; flagged 2026-09-04 as unsafe-by-design vs the manual Option-A
   process. Fix or remove when next touching ui/views/monitor.py.
4. **catchup-fill-credit race root-fix** — the origin of the orphan. Design
   options A/B/C documented (`references/catchup-fill-credit-race-2026-08-28.md`)
   but NOT implemented. The same accident can recur.
5. **Flatten price=0.0 data bug** — parked 2026-09-03, unchanged.
6. **auth2015 income-read scope** — parked, operator's call.
7. **test debt: 13 pre-existing failures** — unchanged (adopt_fill_guard ×4,
   auto_repair_guards, ghost_clearing ×2, inv38, parity_gates_retry,
   require_proof_writers ×2, seal_short_phantom, snap_allocate_gate,
   streamlit test_database_views).
