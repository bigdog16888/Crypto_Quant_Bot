# Daily-Shutdown Self-Healing Startup Architecture

**Status:** Permanent architecture rule
**Established:** 2026-08-26
**Supersedes:** Manual morning-drift diagnosis workflow

---

## The Rule (verbatim)

> This system runs on a machine that shuts down daily (laptop closes overnight,
> meetings, etc). This is normal, expected, routine — NOT an incident. The engine
> WILL be offline for hours at a time, repeatedly, every day. Resting orders
> (grid orders, entries, TPs) WILL fill on the exchange while the engine is down.
>
> On every single startup, the system must automatically:
>
> 1. Pull exchange fill/order history since last shutdown,
> 2. Match fills to `bot_orders` by CID (client order id),
> 3. Credit any uncredited fills to the correct bot's ledger,
> 4. Verify the resulting virtual net matches the exchange's real net per pair, and
> 5. Only then resume normal trading.
>
> This must happen WITHOUT manual intervention or a human diagnosing it each time —
> that is the actual point of the `reconstruct_offline_fills` mechanism and the
> startup-repair CID-verification work.
>
> If the startup barrier still blocks after full automatic reconciliation, THEN it's
> a genuine anomaly worth a human looking at — but routine overnight drift from
> known, CID-traceable fills must self-heal, every time, automatically.

---

## Why This Matters

The machine this runs on is a laptop. It sleeps overnight, during meetings, and
whenever the lid closes. This is the **normal operating condition**, not a failure
mode. Every single morning, the engine comes up to find that resting orders placed
before shutdown have filled on the exchange while it was asleep.

If the startup barrier treats this routine drift as a fatal mismatch and blocks,
a human has to manually diagnose and credit fills every morning. That defeats the
purpose of an autonomous trading system.

The correct behavior: **CID-traceable drift self-heals silently.** Only genuinely
unexplainable divergence (fills with no matching CID, sign conflicts, orphans with
no bot attribution) should escalate to a human.

## The Mechanism

| Step | Component | Location |
|------|-----------|----------|
| 1 | Pull exchange fills since last shutdown | `reconstruct_offline_fills()` |
| 2 | Match fills to `bot_orders` by CID | `_mismatch_explainable_by_cid()` |
| 3 | Credit uncredited fills to correct bot | `startup_repair_verification.py` |
| 4 | Verify virtual net == exchange net per pair | `verify_all_pairs_netting()` |
| 5 | Resume trading only if parity holds | O-9 startup barrier |

## Classification: Reparable vs Truly-Ambiguous

A pair-level mismatch at startup is **reparable** (auto-heal, no human) when:

- Every delta can be traced to a filled exchange order whose CID matches a known
  `bot_orders` row (`CQB_<botid>_*`), AND
- The signed sum of those CID-matched fills exactly accounts for the virtual-vs-
  physical gap, AND
- No sign conflict exists (one-way mode cannot hold both directions).

A mismatch is **truly-ambiguous** (block, escalate to human) when:

- There are filled exchange orders with CIDs that match NO `bot_orders` row
  (unexplained orphans), OR
- The CID-matched fills do NOT sum to the observed gap, OR
- A sign conflict exists between DB and exchange.

## Related Work

- `engine/startup_repair_verification.py` — real CID-based verdict functions
  (`_pair_has_unexplained_orphan`, `_mismatch_explainable_by_cid`)
- `tests/test_startup_repair_verification_real.py` — 7 regression tests
- `engine/hedge_watchdog.py` — O-10 pair-level netting verification
- `engine/oneway_netting.py` — `reconcile_oneway_pair_open_qty` (see known issue)

## Known Issue (2026-08-26)

`reconcile_oneway_pair_open_qty` in `engine/oneway_netting.py` uses
`diff = virtual - physical` and trims when `diff > 0`. For net-SHORT pairs this
sign convention can trim a bot in the wrong direction during startup, and
`seal_trade_state` (startup step 3/8) recomputes `open_qty` from `bot_orders`
which may not sum to the true position. Together these can re-corrupt a manually
corrected ledger on every restart. The startup-repair CID-verification wiring is
the intended permanent fix.
