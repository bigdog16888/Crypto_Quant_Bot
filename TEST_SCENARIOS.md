# TEST_SCENARIOS.md — ADR-002 Hedge Child Bot
**Version:** 1.0  
**Target:** v3.6.0  
**Companion to:** docs/adr/ADR_002_hedge_child_bot.md, TICKETS.md

Run these scenarios after all 10 tickets pass individually.
Each scenario is a complete end-to-end lifecycle check.

---

## SCENARIO-1: Full Hedge Lifecycle (Happy Path)

**Setup:** Parent bot (LONG, `hedge_trigger_step=8`), hedge child (SHORT), no other bots on pair.

**Steps:**

```
1. Parent at step 7 — hedge child in hedge_standby
   ASSERT: child trades.open_qty == 0
   ASSERT: child bots.status == 'hedge_standby'
   ASSERT: no SHORT on exchange for this pair

2. Parent step 8 fills (entry order for step 8 credits via credit_fill)
   ASSERT: maintain_orders signals hedge child entry
   ASSERT: hedge child places SHORT entry order on exchange
   ASSERT: hedge child credit_fill credits the entry fill
   ASSERT: child trades.open_qty > 0
   ASSERT: child bots.status == 'IN TRADE'

3. Parent step 9 fills
   ASSERT: hedge child receives second entry signal
   ASSERT: child open_qty increases by step-9 martingale qty
   ASSERT: child avg_entry_price is weighted average of both entries

4. Parent TP hits (cascade via handle_tp_completion)
   ASSERT: handle_tp_completion detects hedge_child_bot_id is set
   ASSERT: pending_placement TP created for hedge child at child avg_entry_price
   ASSERT: parent resets to Scanning, cycle_id increments
   ASSERT: parent trades.open_qty == 0

5. bot_executor picks up pending_placement TP on next cycle
   ASSERT: GTC limit order placed on exchange for hedge child
   ASSERT: child bot_orders has tp row with status='open'
   ASSERT: child bots.status still 'IN TRADE'

6. Hedge child TP fills
   ASSERT: credit_fill credits the TP fill
   ASSERT: child trades.open_qty == 0
   ASSERT: child reset_bot_after_tp fires → status = 'hedge_standby'
   ASSERT: child cycle_id increments

7. Global netting throughout all steps
   ASSERT: get_pair_virtual_net(pair) matches exchange physical net at every step
   ASSERT: active_positions has NO rows with bot_id=0 at any point
   ASSERT: no REQUIRE_MANUAL_PROOF on either bot at any point
```

---

## SCENARIO-2: Engine Restart Mid-Hedge

**Setup:** Parent at step 9, hedge child IN TRADE with 2 entries. Engine stops. Engine restarts.

**Steps:**

```
1. Engine stops mid-cycle (kill process)
   Exchange state: parent has 9 entries, child has 2 SHORT entries

2. Engine restarts → startup_sync runs
   ASSERT: reconstruct_offline_fills finds no missed fills (nothing happened offline)
   ASSERT: _align_memory_to_ledger restores both bots correctly
   ASSERT: active_positions assigns SHORT to hedge child (not bot_id=0)
   ASSERT: hedge child resumes 'IN TRADE' correctly
   ASSERT: parent resumes 'IN TRADE' correctly
   ASSERT: get_pair_virtual_net matches exchange immediately after startup

3. Engine resumes normal cycling
   ASSERT: no MANUAL GATE, no REQUIRE_MANUAL_PROOF
   ASSERT: hedge child continues managing its SHORT TP
```

---

## SCENARIO-3: Parent TP Fires While Hedge Child Has No Position

**Setup:** Parent at step 9, hedge child was never triggered (hedge_trigger_step not reached in this cycle, or child TP already filled before parent).

**Steps:**

```
1. Parent TP hits → handle_tp_completion fires
   ASSERT: code checks hedge_child_bot_id
   ASSERT: child open_qty == 0 check suppresses break-even TP placement
   ASSERT: no phantom TP order placed for child
   ASSERT: parent resets normally to Scanning
   ASSERT: child stays in hedge_standby

2. Next parent cycle begins cleanly
   ASSERT: no residual state from previous cycle affecting child
```

---

## SCENARIO-4: Hedge Child TP Rejected by Exchange (ReduceOnly Error)

**Setup:** Child has pending_placement TP. Exchange rejects with -2022 (no position to reduce).

**Steps:**

```
1. bot_executor attempts to place child TP → exchange returns -2022
   ASSERT: error is caught and logged (not a crash)
   ASSERT: bot_executor checks exchange physical position for child's pair
   ASSERT: if physical position == 0 → child is safe_wipe_bot'd to hedge_standby
   ASSERT: if physical position > 0 → child flagged REQUIRE_MANUAL_PROOF

2. Monitor shows REQUIRE_MANUAL_PROOF only if real position exists
   ASSERT: no false positive if exchange is genuinely flat
```

---

## SCENARIO-5: Cross-Reduction Suppression Verification

**Setup:** Parent (LONG) and hedge child (SHORT) on XRP. Unrelated SHORT bot `short_xrp` also on XRP.

**Steps:**

```
1. Parent fills entry at step 5
   apply_oneway_entry_cross_reduction is called

   ASSERT: hedge child open_qty NOT changed
   ASSERT: unrelated `short_xrp` open_qty IS reduced by entry delta
   ASSERT: global netting still correct after reduction

2. Hedge child fills entry (SHORT)
   apply_oneway_entry_cross_reduction is called from child's perspective

   ASSERT: parent open_qty NOT changed (child never reduces parent)
   ASSERT: unrelated LONG bots on pair ARE reduced (if any)
   ASSERT: global netting correct
```

---

## SCENARIO-6: Migration Script Idempotency

**Setup:** Bot 10017 with hedge_qty=44.7 in trades. No hedge child yet.

**Steps:**

```
1. Run scripts/migrate_hedge_to_child_bot.py
   ASSERT: child bot created with correct direction, open_qty=44.7
   ASSERT: parent hedge_qty zeroed
   ASSERT: active_positions XRPUSDC SHORT → child bot_id (not 0)
   ASSERT: both bots sealed correctly

2. Run script again
   ASSERT: no second child bot created (idempotent)
   ASSERT: child open_qty still 44.7 (not doubled)
   ASSERT: parent hedge_qty still 0

3. get_pair_virtual_net('XRP/USDC:USDC')
   ASSERT: returns -44.7 (child SHORT)
   ASSERT: matches exchange physical net
```

---

## SCENARIO-7: Global Netting Zero-Mismatch Guarantee

**After tickets 1-10 are all deployed and migration script has run:**

```
1. Start engine
   ASSERT: HEALTHY status within 2 reconciler cycles (no manual intervention)
   ASSERT: all pairs show 0 mismatch in Global Netting Diagnostics

2. Run for 30 minutes
   ASSERT: no new REQUIRE_MANUAL_PROOF states appear
   ASSERT: no bot_id=0 rows in active_positions
   ASSERT: no order_type='hedge' or 'hedge_tp' rows written to bot_orders

3. Let at least one hedge child cycle complete (TP hit → reset → re-enter)
   ASSERT: child returns to hedge_standby cleanly
   ASSERT: next parent cycle starts with child in correct state
   ASSERT: global netting still zero-mismatch
```

---

## SCENARIO-8: Deprecation — No Hedge Order Types Written

**After ticket-10 deprecation sweep:**

```
1. Run engine for 1 full cycle of a hedging bot pair
   Query: SELECT COUNT(*) FROM bot_orders WHERE order_type IN ('hedge','hedge_tp')
          AND created_at > [start_of_run_ts]
   ASSERT: count == 0

2. Query: SELECT COUNT(*) FROM bots WHERE status IN ('HEDGED','HEDGE_EXIT_PENDING')
   ASSERT: count == 0

3. Confirm recompute_invested_from_orders returns 4-tuple for all bot_ids
   for bot_id in [10017, hedge_child_id, 10007, 10008, ...]:
       result = recompute_invested_from_orders(bot_id)
       ASSERT: len(result) == 4
```

---

## Running These Scenarios

The integration test file for scenarios 1, 5, and 7 is:
```
tests/test_hedge_lifecycle.py
```

Scenarios 2, 3, 4 require a running engine instance and are documented as
manual verification steps in `docs/OPERATOR_MISMATCH_RUNBOOK.md`.

Scenarios 6 and 8 are automated and run with `pytest`.
