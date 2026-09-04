# REL-1 Root Cause — Emergency-Liquidation Path Dead on -2015 (2026-09-04)

**Status:** ROOT CAUSE CONFIRMED by live probe + source audit. Fix design proposed — awaiting operator review. Nothing implemented.

**Engine at investigation:** running from `36c119c` (restart 12:29). The running process still contains the dead emergency path until the next restart.

---

## 1. What is broken

`handle_emergency_liquidation` (engine/runner/shutdown.py:102) is the safety net O-3 triggers in a genuine ≥20% drawdown. It has two halves:

| Half | Call | Status |
|---|---|---|
| Cancel all orders | `ex.cancel_orders_by_bot_id(id, pair)` → wrapper `fetch_open_orders` (exchange_interface.py:1092) | **WORKS** (live-proven 2026-09-04 09:08:33 — BNB 10007 TP #346569996 + GRID #346569997 cancelled, DB rows `cancelled`) |
| Close all positions | `ex.exchange.fetch_positions()` (shutdown.py:129) — **ccxt-NATIVE** | **DEAD** — raises `-2015 AuthenticationError` on every call |

## 2. Live probe (2026-09-04 ~12:50, same key the engine uses)

```
=== PROBE 1: ccxt-NATIVE fetch_positions (the handle_emergency_liquidation path) ===
NATIVE FAILS: AuthenticationError binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}

=== PROBE 2: wrapper fetch_positions (raw-request path) ===
WRAPPER OK: [('ETH/USDC', 0.901), ('SOL/USDC', -0.09), ('BNB/USDC', -0.02)]
```

## 3. Root cause (mechanism, not assumption)

The wrapper (`ExchangeInterface`) signs raw requests against **demo-fapi URLs**; ccxt's native request builder targets **production FAPI endpoints**, which the demo key cannot authenticate → `-2015`. Every working engine path (position reconciliation, snapshot, hedge-live-guard) uses the wrapper; this is the **only native-bypass call site in the entire engine**:

```
$ grep -rn "\.exchange\.fetch_positions" engine/*.py engine/runner/*.py   # → 1 hit: shutdown.py:129
$ grep -rc "\.exchange\.fetch_positions" engine/runner/shutdown.py        # → 1
```

Known pattern (2026-07-17, references/ccxt-demo-fapi-incompatibility.md): ccxt-native fails, wrapper succeeds, same key — NOT a permission problem. The Sep 3/4 "storm" was this same call repeated per-bot (~40 failures in 14s) inside the emergency loop.

## 4. Consequences in a REAL emergency (pre-fix)

- Cancels DO execute → all TPs/grids vanish.
- Position closes DO NOT → positions stay open, naked, engine shuts down.
- Worst case: O-3 fires a real alarm, the engine strips its own protection (cancels) but leaves the exposure. That combination is *worse than doing nothing*.

## 5. Proposed fix (exact diff — awaiting sign-off)

```diff
--- a/engine/runner/shutdown.py
+++ b/engine/runner/shutdown.py
@@ duties inside handle_emergency_liquidation, futures branch:
-                    try:
-                        positions = ex.exchange.fetch_positions()
+                    try:
+                        # REL-1 fix (2026-09-04): use the WRAPPER's raw-request
+                        # fetch_positions. ccxt-native calls hit production FAPI
+                        # endpoints; the demo key gets -2015 on every call
+                        # (live-probed 2026-09-04). This was the only native-bypass
+                        # in the engine and made the emergency close path
+                        # dead-on-arrival. Wrapper keys used downstream
+                        # ('symbol', 'contracts') are identical (probe-verified).
+                        positions = ex.fetch_positions()
```

One line. No redesign of the emergency path. Side/qty logic (`side = 'sell' if qty > 0 else 'buy'`, `close_qty = abs(qty)`) is unchanged and correct for both directions (ETH +0.901→sell, BNB −0.02→buy).

## 6. Test plan (new file `tests/test_emergency_liquidation.py`)

1. `test_emergency_close_uses_wrapper_not_native` — mock exchange where native `fetch_positions` RAISES AuthenticationError (replay of the live failure) and the wrapper returns a live position; patch `get_connection`, `reset_bot_after_tp`, `config.DRY_RUN=False`; assert `create_order` called with ('ETH/USDC:USDC', 'market', 'sell', 0.901) and `cancel_orders_by_bot_id` called.
2. `test_emergency_close_short_side` — wrapper returns −0.02 SHORT → `create_order('buy', 0.02)`.
3. `test_emergency_skips_flat_positions` — zero-size → no `create_order`.
4. (Optional tripwire) source-level regression guard: shutdown.py contains no `.exchange.fetch_positions` (prevents silent re-introduction).

Verification after approval: targeted tests → full suite (expect the same 587/13+6 known set) → py_compile → single commit, hash named.

## 7. Interim risk statement (operator decision)

The RUNNING engine (from `36c119c`) still has the dead emergency path **in memory** until the next restart. Options:

- **(a) Restart again after the fix commit** — emergency path becomes live immediately. Costs: another startup barrier pass (~90s), fresh ENGINE_STARTED_AT rebaseline (O-3 handles it by design, D).
- **(b) Let it ride to the next natural restart** — interim exposure: if a REAL ≥20% drawdown fired in that window, the engine would cancel all orders but fail to close positions. On testnet ($9.1k demo, positions: ETH 0.901 + BNB 0.02 + SOL −0.09), bounded and small; a real 20% drawdown between now and next restart is unlikely.

My recommendation: (a) — the whole point of today was making the safety net real; leaving it dead-in-memory for hours contradicts that. But it's your call.

## 8. Self-review

1. **Unverified claims:** none — native failure and wrapper success both live-probed this session; single-call-site claim grep-proven; cancel-half working claim backed by 09:08:33 dated log lines + DB rows.
2. **Contradictions:** none found. The 09:08:30–09:08:44 "storm" is fully explained by this one call looping over ~13 bots (~40 errors) — count matches per-bot repetition.
3. **Scope:** fix-the-class audit done — grep over engine/ shows zero other native `fetch_positions`/`create_order`/`cancel_order` bypasses in engine code paths (remaining `.exchange.` hits are market-metadata reads or wrapper-internal delegation).
4. **Mechanism:** traced end-to-end with raw probe output, source lines, and dated incident logs.
5. **Test gaps:** the emergency path has NO test today (new file adds 3–4). Full path with a REAL exchange cannot be tested live without mutating positions — mocked functional tests + the continuously-live-proven wrapper (every cycle's position reconcile uses it) is the honest maximum.
6. **Reversibility:** one-line diff, git-revertable.
