# OVERNIGHT REPORT — NIGHT 2 (2026-09-23 → 09-24)

**Operator sleep window: 2026-09-23 ~23:00 → 2026-09-24 ~04:00 (UTC+8)**
**Engine: OFF the entire window (AGENTS.md Rule 2). UI (localhost:8501) read-only.**

---

## 1. Task 1 — UI Two-Tier Status Distinction: ✅ COMPLETE — commit `9ba4cad`

`fix(ui): separate tier-1 live exchange parity from tier-2 historical ledger advisory`

**Changes (2 files, +25/−14):**
- `engine/health.py` — tier-2 imbalance no longer escalates `system_status` to
  MISMATCH when tier-1 drift is clean. New fields: `tier1_status`,
  `tier2_status` (`HEALTHY` / `LEDGER_ADVISORY`), `worst_gap_usd` computed from
  tier-1 only. Escalation rule: MISMATCH requires `mismatched_pair_count > 0`
  (tier-1 drift). Tier-2 imbalance on dormant bots ⇒ `LEDGER_ADVISORY`.
- `ui/views/monitor.py` — top ribbon now renders `tier1_status`; a separate
  `ℹ️ LEDGER ARCHIVE ADVISORY` caption renders only when
  `tier2_status == "LEDGER_ADVISORY"`.

**Raw DOM evidence (live, `python scripts/tools/inspect_live_ui.py`):**
```
⚡ Status
🟢 HEALTHY
ℹ️ LEDGER ARCHIVE ADVISORY — Dormant bots have historical ledger imbalances (migration-era dust). Exchange parity is HEALTHY.
```
Before the fix the same live state rendered `🔴 MISMATCH` on the ribbon.

---

## 2. Task 3 — GTR Lock Display: ✅ COMPLETE — commit `953de15`

`feat(ui): add GTR lock status indicator to live monitor`

**Change (1 file, +56/−19):** `ui/views/monitor.py`
- Header fragment derives GTR state from tested engine sources only:
  - `is_engine_running()` (`engine/shutdown_control.py:99` — SocketLock port 19888 + PID)
  - `manual_proof_bots` / `stuck_cascade_bots` from `get_system_health()` output
- State model (matches GTR design, `cycle_loop.py:993-1007` — GTR runs every
  10 engine cycles and can auto-heal drift but NOT `REQUIRE_MANUAL_PROOF`):
  | Condition | Display |
  |---|---|
  | engine off | `🛡️ GTR Lock: ⚪ DISENGAGED — engine off — GTR not running` |
  | engine on, no locked bots | `🛡️ GTR Lock: 🟢 ACTIVE — running every 10 cycles (pid N)` |
  | engine on, ≥1 manual-proof/stuck bot | `🛡️ GTR Lock: 🔴 LOCKED — N bot(s) require manual proof / stuck` |
- Also fixed a 20-space indentation artifact in the fragment data dict (cosmetic).

**Raw DOM evidence (live, engine currently off):**
```
🛡️ GTR Lock: ⚪ DISENGAGED — engine off — GTR not running
```

---

## 3. Task 2 — Archive 8 Migration-Era Phantom `bot_orders`: 🛑 BLOCKED — task premise is wrong

**No script was written. No DB write was performed.** Root-cause investigation
proved the proposed remedy (`archive_migration_dust.py` moving `bot_orders`
rows into `bot_orders_archive`) **cannot clear the tier-2 `LEDGER_ADVISORY`
imbalance**, because tier-2 is not computed from `bot_orders` at all.

### Proof chain (all read-only, against live `crypto_bot.db`, commit `953de15`)

1. **Tier-2 net source is `exchange_fills`, not `bot_orders`.**
   - `engine/health.py:298-301` — tier-2 sums `compute_bot_position(bot_id,
     cycle_floor=0, cycle_ceiling=None)` over all bots with fills.
   - `engine/position_ledger.py:94-98` — `compute_bot_position` →
     `_fetch_fills_for_bot` → `SELECT ... FROM exchange_fills WHERE bot_id = ?`.
   - `engine/health.py:218-221` — the `bot_has_fills` gate itself counts
     `SELECT COUNT(*) FROM exchange_fills WHERE bot_id = ?` (comment at line
     208: "NOT bot_orders (which may have phantom fills with filled_at=0)").

2. **There are no "unowned" `bot_orders` rows to archive.**
   ```
   bot_id IS NULL:                    0
   bot_id not present in bots:        0
   ```
   Dormant bots' `bot_orders` rows all carry valid bot_ids (10008: 219 rows,
   10017: 31, 10018: 3, 10022: 2, 100000: 26, 100317: 21, 100318: 10, 100323: 10).

3. **Tier-2 values reproduce exactly from `exchange_fills` full history.**
   Side-signed full-history fill sums per pair vs the health check's
   `ledger_net` (physical = 0 on all pairs):
   ```
   pair        fill-sum     health ledger_net   match
   ETHUSDC     +8.097       +8.097              EXACT
   LINKUSDC    +454.84      +454.84             EXACT
   XAUUSDT     +0.105       +0.105              EXACT
   BTCUSDC     +0.014       +0.020              per-bot symbol-filter diff
   SOLUSDC     +45.78       +52.81              per-bot symbol-filter diff
   ```
   (The two non-exact pairs differ only by which fills pass each bot's
   symbol-normalization filter inside `compute_bot_position` — same source
   table either way.)

4. **These are not duplicate fills.** Dedup-shape
   `(exchange_order_id, fill_ts, qty, price)` scan: **0 duplicate groups** on
   all 5 imbalanced pairs. They are genuine historical fills from dormant/migration
   bots (e.g. LINK bot 100320, `source='backfill'`, entry-side grid fills in
   Aug 2026 with no matching close).

5. **Archiving `bot_orders` would therefore change nothing in tier-2** (proven
   by 1+2+3), and the only way to make the number zero would be to delete or
   alter **`exchange_fills` rows — the immutable append-only position-truth
   log (AGENTS.md domain facts, Rule 2 safety boundary #1).** That is a
   forbidden action without explicit operator sign-off, and it is the wrong
   remedy: the imbalance is a *real historical fact* (dormant bots traded and
   the exchange position is flat), not a phantom.

### What the 5 imbalanced pairs actually are

| Pair | ledger_net | Physical | Nature |
|---|---|---|---|
| LINKUSDC | +454.84 | 0 | bot 100320 backfilled entry-side fills (Aug), never closed in ledger |
| SOLUSDC | +52.81 | 0 | bot 10008 long history (150 fills, cycles 1–88, oldest 2025-12) |
| ETHUSDC | +8.097 | 0 | 6 dormant bots' residual nets |
| BTCUSDC | +0.02 | 0 | sub-quantity dust |
| XAUUSDT | +0.105 | 0 | sub-quantity dust |

### The correct fix (DESIGN ONLY — not applied, needs approval + diff)

Add a **tier-2 dormant-bots exclusion gate** in `engine/health.py`: when every
bot on a pair is `is_active = 0` **and** the exchange physical position is 0,
the full-history fill residue is *by definition* closed on the exchange —
report it as informational (per-bot archive note) instead of `ledger_imbalance`.
Active-bot pairs keep the current strict tier-2 check unchanged. This is the
"canonical" solution: no raw SQL, no data mutation, truth-preserving.
Alternative considered and rejected: `manual_whitelists` adjustments — they
offset `physical_net` (health.py:355-357), not `ledger_net`, so they cannot
target tier-2 at all.

---

## 4. NEW FINDING (not in task list) — SUI pair is a health blind spot + 1 unrecorded fill

Discovered while verifying Task 2's data:

1. **SUIUSDC is entirely absent from tier-2 output** (not in
   `netting_status_per_pair` at all). Cause: `engine/health.py:165-168` only
   scans bots with `is_active = 1 OR trades.open_qty > 0.0001`. All 3 SUI bots
   (10018, 100000, 100323) are `is_active=0` with `open_qty=0.0` — so **no
   fill-history check runs for SUI even though it has 80+ `exchange_fills`
   rows** (side-signed sum ≈ +335.9, exchange physically flat).
2. **Forward-test residue, exchange vs DB mismatch (order 185035956):**
   - Exchange (live `fetch_order`): `status=filled, side=BUY, amount=11.8, filled=11.8`
   - DB `bot_orders` row: `status=open, filled_amount=0.0` (stale)
   - `exchange_fills`: **no row for 185035956** (fill never credited)
   - TP order 185035955: exchange `canceled`, DB still `open` (stale but
     harmless — it never filled)
   - Net effect on exchange: flat (the 11.8 BUY was evidently closed by a fill
     also absent from `exchange_fills`). The ledger therefore over-states SUI
     long history by ~11.8+, but because SUI is in the blind spot (item 1),
     **nothing in the health check can ever see this**.
   - **No live risk**: SUI exchange position is flat, TP already canceled,
     no resting orders (verified via `fetch_order` on both orders).

**Decision needed (morning):** (a) fold SUI-style all-dormant pairs into the
tier-2 gate fix from §3 (which would surface this as `LEDGER_ADVISORY`);
(b) reconcile the missing 185035956 fill via the tested forensic path
(reconciler / offline-fill reconstruction) vs leave dormant and document.
Both are DB-touching → require explicit GO per Rule 8.

---

## 5. Test Invariant — ✅ HELD

Command (fresh subprocess, commit `953de15`):
```
cd D:/Crypto_Quant_Bot && python -m pytest tests/ --ignore=tests/test_playwright_ui.py -q --no-header
# raw tail:
SKIPPED [1] tests\test_compute_position_state_zero_writes.py:28: compute_position_state() not implemented in engine.ledger
SKIPPED [1] tests\test_compute_position_state_zero_writes.py:118: compute_position_state() not implemented in engine.ledger
703 passed, 2 skipped, 10 warnings, 4 subtests passed in 126.20s (0:02:06)
```
**703 passed, 0 failed, 2 skipped** — meets the 703+ invariant. (Run after
`9ba4cad` + `953de15` landed, i.e. includes both UI changes.)

---

## 6. State for morning

- **Git:** `953de15` on main (2 new commits this window: `9ba4cad`, `953de15`;
  ahead of origin by 63). Working tree: `AGENTS.md` modified (stale stage header
  from earlier sessions — docs-only, uncommitted by design), ~34 untracked
  scratch files at repo root (`check_*.py`, `debug_*.py`, …) — repo hygiene
  says these belong in `%LOCALAPPDATA%\Temp`; **deletion needs approval** (write).
- **Engine:** STOPPED (untouched). **Exchange:** flat, no resting orders on any
  pair checked (SUI verified; full position fetch returned zero non-zero
  positions). **UI:** streamlit still running at localhost:8501 (read-only).
- **Open (not silently resolved):**
  1. Task 2 fix: tier-2 dormant-bots gate in `health.py` — diff to be presented,
     approval required.
  2. SUI blind spot + uncredited fill 185035956 — decision (a)/(b) above.
  3. AGENTS.md stage header refresh (docs).
  4. Scratch-file cleanup (repo hygiene).
  5. Pre-existing `test_gate_blocks_when_require_manual_proof` — passes in this
     run (703 green), consistent with "fails identically at clean HEAD" only
     under specific prior conditions; **unconfirmed** root cause, left alone.

## 7. Boundary compliance
- No engine start, no orders placed/canceled, no exchange mutations.
- Zero writes to `crypto_bot.db` (all queries `mode=ro`).
- No changes to any safety gate. Only 2 committed code changes, both UI/health
  display-layer, both covered by the full green suite.
