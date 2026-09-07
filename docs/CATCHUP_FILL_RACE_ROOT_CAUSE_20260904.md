# Catchup Fill-Credit Race — Root Cause (code-level, complete)

**Date:** 2026-09-04
**Status:** ROOT CAUSE COMPLETE — fix design below, NOT implemented. Awaiting operator review.
**Supersedes:** docs/ETH_LINK_CATCHUP_FILL_RACE.md (2026-08-28 — evidence + options A/B/C; its mechanism sketch is now refined: the actual writer of the racing terminal status is identified, and the loss choke-point is `credit_fill`, not the reset paths alone).
**Incidents:** ETH orphan (frozen 8 days, closed 2026-09-04 +$87.56), LINK freeze (Aug 28, still frozen).

---

## The root cause, in one paragraph

The hedge-child catchup lifecycle (`engine/bot_executor.py:3239` INV-30) places catchup
entry orders whose fills can arrive **after** the engine has already moved their
`bot_orders` rows to a terminal status — or whose sibling rows make
`credit_fill`'s step-saturation guard misclassify them as duplicate chase retries
and `auto_close` them at zero. When the real fill event then arrives,
`_credit_fill_internal` (engine/ledger.py:399-404) **refuses to record it** because
the row's status is terminal, AND the `fill_claims` dedup slot was inserted
**before** that refusal (engine/ledger.py:345-357) so every later retry of the same
order_id is also refused. The refused fill falls through to anonymous attribution,
which is disabled (`ALLOW_FORENSIC_ADOPT=False`) — silently dropped. The DB now
understates the physical position, virtual net diverges from exchange, and O-10
correctly freezes the pair.

## The two failure modes (both observed)

### Mode 1 — step-saturation false-positive on catchup (ETH, bot 100325)

The step-saturation guard (`ledger.py:434-497`, "ORDER-ID-PROOF STEP SATURATION
GUARD [v2.5]") exists to stop GTX chase-retry double-credits: retries place NEW
exchange orders for the same logical (bot, step, cycle), so if the first attempt
filled and was credited, later retries must not re-credit the same step quantity.

The guard classifies "new order for an already-partially-credited step" as a
duplicate. **But a catchup entry is not a chase retry** — INV-30 places it
precisely because the child's step is UNDER-covered (delta = parent_step_qty −
child_step_qty > 0). The catchup order intends a NEW fill that increases physical
exposure. When the guard fires on it:

```
bot_orders row: CQB_100325_ENTRY_5_2_CATCHUP  status=auto_closed  filled=0.000
notes: STEP_SATURATED:already_credited=0.197000,capacity=0.490350
exchange truth: order 402977972* filled 0.467
```

(*example ID; actual exchange order_id from the 08-28 incident evidence.)

The guard's arithmetic is self-consistent (0.197 + 0.467 > 0.490 cap) but its
**premise is wrong**: already_credited=0.197 came from a sibling catchup row
(`ENTRY_5_1_CATCHUP`, filled 0.352 — itself reset_cleared later), not from a
duplicate of THIS order. The exchange filled both orders; the DB recorded neither
(the 5_1 row's fill survived in the row but its `reset_cleared` status excludes it
from every virtual-net reader — database.py:321/359/1803 etc.). Physical +0.467
against virtual 0 → divergence.

Note the ETH row evidence (DB, 2026-09-04):
- `CQB_100325_ENTRY_5_2_CATCHUP_17878` → `auto_closed`, filled=0.000, notes=STEP_SATURATED (Aug 27 16:07)
- `CQB_100325_ENTRY_5_1_CATCHUP_17878` → `reset_cleared`, filled=0.352 (Aug 27 16:09)
- `CQB_100316_ENTRY_4_1_CATCHUP_17878` → `reset_cleared`, filled=0.054/0.095 (Aug 28 / Sep 2 re-runs)
- `CQB_100316_ENTRY_4_2_CATCHUP_17878` → `auto_closed`, filled=0.000, notes=STEP_SATURATED (Aug 28 09:53)

INV-30 does NOT cancel the original step order before placing the catchup — it
computes delta from open/placing rows and live hedge qty only (bot_executor.py
~3152-3230: SELECT … status IN ('open','new','placing','cancelling') + LIVE-GUARD
exchange-net adjustment). So sibling rows coexist by design; the guard treats that
coexistence as duplication.

### Mode 2 — wipe/reset removes credit history, late fill lands on stale row (LINK, bot 100320)

LINK's frozen rows (08-28 evidence, DB cleared by SYSTEM_WIPE at 16:51 Aug 28):
- `CQB_10020_GRID_1_9` → `open`, filled=0.0 — exchange filled 140.33
- `CQB_100320_ENTRY_1_2_CATCHUP` → `partially_filled`, 0.51 — exchange filled 69.22
- `CQB_100320_ENTRY_1_9_R1787877974` → `open`, filled=0.0 — exchange filled 68.72 (12 partials, 6s burst)

The SYSTEM_WIPE path (`_reset_bot_after_tp_internal` with action_label
'SYSTEM_WIPE', database.py:1780-1817) bulk-`auto_closed` all open rows AND
`DELETE FROM fill_claims WHERE bot_id = ?` (line 1817). A fill event arriving
after the wipe:

1. `credit_fill` finds the row (`open` — not terminal) → claim insert OK →
   step-saturation: sibling `ENTRY_1_2_CATCHUP` credited 69.22 for step 2;
   this row is step 9 with capacity 68.72×1.05=72.156 — 69.22+68.72 > 72.156 only
   if same step; if same step → STEP_SATURATED refuse; if different step → credits.
   The observed outcome (`open`, filled=0.0, exchange-filled) says refuse happened.
2. Or the wipe itself terminal-statused the row between placement and fill
   arrival (bulk `auto_closed` at 1815) → terminal refusal at 399-404.

Either sub-path ends at the same choke point: the fill is refused, the claim is
burned, retries dead-end, anonymous attribution is off → fill lost.

### The shared choke point (both modes)

`engine/ledger.py` `_credit_fill_internal`:

```python
# ── INV-20: fill_claims singleton guard ─────────────
_claim_result = conn.execute(
    "INSERT OR IGNORE INTO fill_claims (bot_id, order_id, caller, claimed_at) ...")
if _claim_result.rowcount == 0:
    return False                       # ← burns slot BEFORE any row-status check
...
# Only reject truly administrative terminal statuses
if current_status in ('reset_cleared', 'auto_closed'):
    logger.debug(...)                  # ← DEBUG level: invisible in ops
    return False                       # ← the fill is dropped here
```

And the terminal refusal is **asymmetric with cancellation**: a fill on a
`cancelled` row triggers `[FILL-RESURRECTION]` (line 407-411) and IS credited —
the codebase already acknowledges that late fills on "dead" rows are real money
and must be recorded. `reset_cleared`/`auto_closed` get the opposite treatment
with no resurrection path.

Fall-through for refused fills: `_handle_fill_with_pending_retry` only parks fills
whose **row doesn't exist yet**; a refused-because-terminal fill is not re-queued,
and `_attribute_anonymous_fill` requires ALLOW_FORENSIC_ADOPT (default False) —
with it off, `[ANONYMOUS-NO-ADOPT]` → return False → silent drop
(ws_event_handlers.py:55-74, 335+).

## Why the existing protections didn't catch it

| Protection | Why it didn't fire |
|---|---|
| Startup CID-verification (barrier step 8.5) | Only heals **downtime** fills (engine off). This race is live-trading; parity passes at startup. |
| `WS-FILL-CATCH` (bot_executor.py:3303-3309) | Only drains rows stuck in `cancelling` status — not `open` rows raced by reset/wipe, not terminal-statused rows. |
| Pending-fill retry queue (ws_event_handlers.py:55-112) | Only retries when the DB **row doesn't exist** (credit_fill returned False due to no-row). Terminal-refused fills aren't parked. |
| FILL-RESURRECTION (ledger.py:407) | Applies to `cancelled` rows only. |
| O-10 freeze | Fired correctly — downstream detection, not prevention. |

## Fix design (NOT implemented — operator review)

### Fix 1 (core): credit-before-refuse — make terminal-status fills recordable

Replace the silent terminal refusal with a **resurrection path** mirroring
FILL-RESURRECTION, safe against double-credit by the existing protections that
make resurrection idempotent:

```python
if current_status in ('reset_cleared', 'auto_closed'):
    # Late fill on a terminal row is REAL exposure (physical grew; ledger must follow).
    # Record fill data on the row WITHOUT resurrecting its virtual-net contribution:
    #   filled_amount/price/filled_at are preserved (audit + exchange-truth),
    #   status moves to a NEW terminal-but-filled state, or stays terminal with
    #   the fill recorded.
    logger.warning(f"[FILL-LATE-TERMINAL] ...exchange fill {cumulative_qty} on
                    terminal row {order_id} — recording, not resurrecting.")
    conn.execute("UPDATE bot_orders SET filled_amount=?, price=?, filled_at=?, updated_at=? WHERE id=?",
                 (cumulative_qty, avg_price, actual_fill_ts, int(time.time()), db_id))
    # Do NOT touch trades.open_qty here — the row's terminal status keeps it out of
    # virtual net; GTR/SNAP detect the virtual-vs-physical gap and adopt it via the
    # existing adoption paths (single-candidate logic, wipe_proof audited).
    return 'recorded_terminal'  # caller distinguishes from True/False
```

The KEY design decision: **record the fill on the row, but do NOT un-terminal it
and do NOT increment open_qty.** The physical position grew; the ledger row now
carries the truth; the ADOPTION of the qty into virtual net is GTR/SNAP's job
(single-candidate + wipe_proof — the paths designed for exactly this). This
avoids: double-credit (open_qty untouched; step-saturation irrelevant since we
don't credit a "step"), false-parity (virtual still reflects cleared intent),
and silent loss (row shows the fill; audit trail intact).

### Fix 2: move the fill_claims insert AFTER the row-status checks

Claims should only burn on a real credit attempt, not on refusals. Reorder:
row lookup → terminal/refusal checks → claim insert → credit. Any refused fill
keeps its dedup slot so a later legitimate path (Fix 1's recording, GTR adoption,
manual proof) can still act.

### Fix 3: exempt catchup orders from step-saturation auto_close (Mode 1)

The guard's premise (chase-retry duplicate) doesn't hold for INV-30 catchups.
Options (pick one):
- **3a**: exclude CIDs containing `_CATCHUP_` from the saturation-refuse branch
  (still check — just refuse without auto_closing; leave the row open so the fill
  can credit).
- **3b**: check exchange state before auto-closing: `fetch_order(order_id)` — if
  already filled/partially filled on exchange, record the fill (Fix 1 path) instead
  of auto-closing at zero.

### Fix 4 (Mode 2): don't wipe fill_claims on reset — or re-derive from bot_orders

`DELETE FROM fill_claims WHERE bot_id` (database.py:1817, 4826; ledger.py:1063)
removes the dedup memory exactly when late fills are most likely (post-wipe).
Options: stop deleting (claims are tiny; 30-day prune already exists), or accept
the deletion but rely on Fix 1+2 (the fill will be recorded regardless of claim
state — claims only dedup, they don't gate recording under Fix 1).

### What I recommend

Fix 1 + Fix 2 unconditionally (choke-point hardening, no behavior change for
happy path). Fix 3a (simplest catchup exemption). Fix 4 as "stop deleting claims
on reset" — one-line removals of the three DELETEs. Total blast radius: ledger.py
(one function region), database.py (two deletes), tests.

## Replay test (the acceptance gate — TDD style)

`tests/test_catchup_fill_race_replay.py` (written, red-first — runs after the fix):
replays BOTH modes with incident-shaped rows:

- **Mode 1 (ETH shape)**: sibling catchup credited 0.197 for step 2 cycle 5;
  new catchup order (amount 0.467) arrives with exchange fill 0.467 →
  CURRENT behavior: guard auto_closes at 0 + fill refused (assert the bug), then
  post-fix: fill recorded on row (filled_amount=0.467), row stays terminal, open_qty untouched, claim NOT burned.
- **Mode 2 (LINK shape)**: row `open` step 9, wipe deletes claims + terminal-statuses
  siblings, late fill 68.72 arrives → CURRENT: refused + claim burned + dropped;
  post-fix: recorded on row, GTR-adoption expected (test asserts recording, not adoption).

Test uses tmp DB + real `save_bot_order`/`credit_fill` code paths
(conftest forces WriteQueue bypass — no engine interaction, no live DB).
```
PASS criteria: [post-fix] filled_amount recorded on terminal rows; open_qty
unchanged; fill_claims empty for refused-then-recorded order; [current] asserts
the bug reproduces (red) before the fix (green) after.
```

## Test-hazard note (why tests are safe to run with the engine live)

The three cwd-writing tests (test_advanced_logic, test_bot_lifecycle,
test_startup_barrier_race) write `engine.emergency`/`engine.pid`/delete
`last_shutdown.ts` in cwd — none of these are read by a RUNNING engine
(last_shutdown.ts is startup-only; engine.emergency/pid are written by tests,
not watched at runtime). DEPLOY-OUTDATED watches only engine/ scripts/ ui/
config/ for .py/.json mtimes — tests/ is not scanned, tmp DBs are per-test.
Verified safe; still run via `python -m pytest tests/test_catchup_fill_race_replay.py -v`.

## What this does NOT fix (honestly)

- The freeze guard for O-10 still fires on any residual divergence — correct behavior.
- GTR/SNAP adoption of recorded-but-terminal fills: if GTR's single-candidate logic
  can't attribute the fill to exactly one bot, REQUIRE_MANUAL_PROOF still fires.
  This design deliberately leaves attribution to the audited adoption paths
  rather than auto-crediting open_qty.
- The pending-fill queue still doesn't retry terminal-refused fills (Fix 1 makes
  the refusal itself record, making retry unnecessary for this class).
- The UI Close-Orphan button fire-and-forget design (separate item).

## Evidence index (raw)

- DB rows (queried 2026-09-04): catchup rows with STEP_SATURATED notes (100325
  ENTRY_5_2_CATCHUP auto_closed@0.000; 100316 ENTRY_4_2_CATCHUP auto_closed@0.000),
  reset_cleared catchups with preserved fills (100325 5_1 0.352; 100316 4_1 0.054/0.095; 100316 4_4 0.320).
- `ledger.py:345-357` claim insert; `:399-404` terminal refusal; `:407-411` FILL-RESURRECTION (cancelled only);
  `:434-497` step-saturation guard + auto_close at zero.
- `bot_executor.py:3239-3267` INV-30 catchup placement (no sibling cancel);
  `:3287-3309` cancelling-buffer WS-FILL-CATCH precedent.
- `database.py:1780-1817` SYSTEM_WIPE bulk auto_close + `DELETE FROM fill_claims`;
  `:4826` reconcile-wipe same pattern; `ledger.py:1063` reset-after-TP claim wipe.
- `ws_event_handlers.py:55-112` pending-fill queue (no-row race only); `:335+`
  anonymous attribution gated by ALLOW_FORENSIC_ADOPT=False.
- 08-28 incident evidence: docs/ETH_LINK_CATCHUP_FILL_RACE.md (exchange fills vs DB).
- LINK exchange sweep: order 147090420 `CQB_100320_ENTRY_1_9_R1787877974` filled 68.72 @ 11.804 (12 partials), status filled — exchange truth; DB row lost it.
