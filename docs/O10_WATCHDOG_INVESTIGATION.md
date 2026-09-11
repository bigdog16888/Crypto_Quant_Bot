# O-10 Hedge-Engagement Watchdog — Startup Transient Investigation

Date: 2026-08-21 · Task: t_b61a426d · Status: **root cause found — fix NOT trivial, recommendation below**

## Verdict

The task's stated hypothesis is **wrong**. The watchdog did not see `child_qty == parent_target`
and mis-flag it. At freeze time the child's `trades.open_qty` was **0**, not 5.04.

The `parent_target 5.04 == child_qty 5.04, delta 0` log line is **not the watchdog's read** — it is
`_signal_hedge_child_entry`'s saturation log (`bot_executor.py:5159-5163`), which reads a
*different table* (`bot_orders`, including pending order amounts). The watchdog
(`hedge_watchdog.py:_child_offset_ok`) reads `trades.open_qty`, which only moves when a fill is
**credited**. Two data sources, two answers, in the same cycle.

## Root cause (three compounding defects)

1. **Data-source mismatch.** Signal reads `bot_orders` (pending `amount` + `filled_amount`);
   watchdog reads `trades.open_qty` (credited fills only). An entry order placed-but-unfilled
   makes the signal say "saturated, delta 0" while the watchdog says "child holds nothing →
   not offsetting → freeze."
2. **No grace window — freeze is immediate.** `verify_hedge_engagement` runs in the *same
   maintain_orders cycle* that `_signal_hedge_child_entry` placed the entry (2 seconds later in
   the SOL case). No fill can possibly be credited yet. `HEDGE_ENGAGE_TIMEOUT_SECONDS = 300`
   is defined (line 22), plumbed through `get_config` (line 35), read into `engage_timeout`
   (line 112) — and **never used**. It is a dead variable. The code comment at lines 140-144
   even asks "Apply a grace window…?" and answers "Simpler, deterministic: we freeze
   immediately." That choice is the bug.
3. **Re-freeze spam.** Once frozen, the parent is still `>= hedge_trigger_step` on every
   subsequent cycle, so the watchdog re-fires and re-writes `last_error`/`last_error_time`
   every ~10s (73 CRITICAL lines total: 10 SOL + 63 gold).

## Reconstructed timeline (SOL, from engine.log)

| Time | Event |
|---|---|
| 08:57:50 | Engine start #1 of the day |
| 08:58:10 | STARTUP-BARRIER plausibility-blocks bot 100324: exchange −10.74 vs DB flat (+0.00) |
| 08:58:45.238 | `[INV-30]` under-hedge drift 5.04 for child 100324 → placing catch-up entry |
| 08:58:45.505 | Order 396594663 NEW (`CQB_100324_ENTRY_26_1_CATCHUP_178727`) |
| 08:58:47.100 | Signal: "step 7 saturated (parent_target=5.04, child_step_qty=5.04, delta=0)" — from `bot_orders` |
| 08:58:47.102 | **O-10 HEDGE-FREEZE #1** of parent 100001 — watchdog read `trades.open_qty=0` |
| 08:58:47→09:00:12 | 10 freezes, one per cycle |
| 09:00:20→27 | Order 396594663 fills in ~10 partials → FILLED 5.04 @ 88.16 |
| 09:00:27.151 | `credit_fill` returns False (DB-row race) → parked in retry queue |
| 09:13:09 | `[PENDING-FILL-EXHAUSTED]` after 762s → **child 100324 escalated to REQUIRE_MANUAL_PROOF** |
| 09:08:58 | Parent 100001 AUTO-CLEAR by parity_gates (pair net matched exchange) |

Gold case (10019 → child 100319) is the same mechanism: child entry filled 0.063 but the
watchdog froze the parent 63× (09:40:34→09:45:41) while the fill credit lagged; child 100319
`trades.open_qty` is 0.063 *now*, direction LONG vs parent SHORT (correctly offsetting) — yet
parent 10019 remains frozen.

## Corrections to the task body

- "child's open_qty already matched the parent's target" — **no.** It was 0 at freeze time; the
  delta=0 line came from the signal's `bot_orders` read, not the watchdog.
- "no bots locked (0 bots in REQUIRE_MANUAL_PROOF from hedge freeze)" — **wrong, and this needs
  operator attention.** Current DB (read-only query, 2026-08-21 ~10:10):
  - bot 100001 `short sol` — REQUIRE_MANUAL_PROOF, `last_error=HEDGE_ENGAGE_FAILURE:child_not_offsetting`
  - bot 10019 `short gold` — REQUIRE_MANUAL_PROOF, `last_error=HEDGE_ENGAGE_FAILURE:child_not_offsetting`
  - bot 100324 `short sol_hedge` — REQUIRE_MANUAL_PROOF (via PENDING-FILL-EXHAUSTED + startup drift note)
  - Engine startup at 09:55:42 and 09:59:49 **FAILED** parity verification (SOL sign mismatch
    DB=+5.18 vs exchange=−5.89 on 100324) — engine did not complete startup as of last log line.

## Secondary finding: engine-halt escalation never armed

`_count_engine_hedge_failures` requires ≥2 parents frozen *simultaneously* in the 24h window.
The two freeze waves were 42 min apart and parent 100001 was auto-cleared at 09:08:58, so the
count never reached 2. If the gold wave had started before the SOL auto-clear, the engine would
have halted on a transient. The halt threshold is fragile by design; worth a separate review.

## Recommendation (fix is NOT trivial — investigation only, no code changed)

Preferred: **Option B′ + A combined**, in `engine/hedge_watchdog.py`:

1. **Align the watchdog's engagement read with the signal's data source.** `_child_offset_ok`
   should count the child's hedge-step position from `bot_orders` (same aggregation as
   `bot_executor.py:5020-5028`: pending `amount` for open orders + `filled_amount` otherwise),
   not `trades.open_qty`. Invariant: *if the signal just logged "saturated, delta 0" for the
   child, the watchdog must agree the hedge is engaged.* Keep the direction and
   child-status checks.
2. **Actually use `engage_timeout`.** Only freeze when the child's entry attempt is older than
   `HEDGE_ENGAGE_TIMEOUT_SECONDS` (via `bot_orders.created_at` / `filled_at`, both present in
   schema) or when no entry order exists at all. This is the grace window the module docstring
   already promises ("within a reasonable grace window").
3. **Stop the re-freeze spam:** in `bot_executor.py:4917`, skip the freeze UPDATE when the
   parent is already REQUIRE_MANUAL_PROOF with the HEDGE_ENGAGE_FAILURE marker (log once).

Option C alone (recency via `trades.updated_at`) does not exist — `trades` has no `updated_at`
column (schema verified) — and would not fix the data-source mismatch.

Test anchor: `tests/test_hedge_lifecycle.py` already exercises the signal path; add a case
"entry placed this cycle, unfilled → watchdog must NOT freeze; unfilled for >engage_timeout →
freeze."

Worktree note: `engine/hedge_watchdog.py` is byte-identical between main
(`refactor/inv31-bot_executor-writequeue`) and the INV31 worktree (`inv31-writequeue`), so the
fix applies to both. Main repo currently has unrelated uncommitted changes
(`engine/ground_truth_reconciler.py`) — do not bundle.
