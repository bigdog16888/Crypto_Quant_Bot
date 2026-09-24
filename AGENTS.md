# AGENTS.md — Crypto_Quant_Bot AI Agent Operating Rules

**THIS FILE IS THE FIRST THING ANY AI AGENT READS BEFORE TOUCHING THIS REPO.**
If your instructions conflict with this file, this file wins. Every rule here was
learned from real damage.

**Current stage (2026-09-23):** engine v3.5, git at `41c0742` on main (ahead 58, behind 0, pushed). Two-tier health check + dual-write guard live; 8 P1/P2 anomalies + 2 follow-ups open (§Open Items). Engine STOPPED. Full state: `PROJECT_STATUS.md`.

**Authoritative files** (conflict precedence: CODEBASE_GUIDE.md > PROJECT_STATUS.md
> everything else):
- `CODEBASE_GUIDE.md` — single authoritative codebase guide (read before touching code)
- `PROJECT_STATUS.md` — live state snapshot + handoff note
- `docs/ARCHITECTURE_v3.5.md` — architecture reference
- `.clinerules/adversarial_review.md` — two-pass adversarial review protocol
  (mandatory for non-trivial changes)
- `docs/adr/` — decision records (ADR-011: one-way hedge design conflict)
- `docs/EXTERNAL_REVIEW_GUIDE.md` — entry point for external review agents

---

## RULE #0 — ONE-WAY MODE (read before touching ANY order/position code)

**THE BINANCE ACCOUNT IS IN ONE-WAY MODE. NOT HEDGE MODE. NEVER HEDGE MODE.**

- Single net position per symbol. Positive = LONG, negative = SHORT.
- `positionSide` is always `BOTH` — carries zero directional info. **Never send it.**
- Direction comes from `sign(positionAmt)`. Close via `side + reduceOnly=True`.
- Multiple bots on one pair net out on the exchange (parent + hedge child fills are
  algebraic sums — ADR-011). Virtual per-bot LONG/SHORT tracking is internal
  accounting only; it never maps to exchange positions.
- The exchange sees only the NETTED position across all bots on a pair — per-bot
  `open_qty` ≠ pair-level net. Always compute the signed sum of per-bot contributions.

---

## Standing operator rules (1–13, binding)

**1. Closed-language ban.** Never call a task/fix/session "complete", "closed", or
"resolved" while ANY item is open/pending/deferred/in-fail-loop. If unresolved, say
**"IN PROGRESS — N items open"**.

**2. Proof before claiming a fix works.** Before any success claim, show:
(a) the actual before/after diff, (b) actual command output of verification — never
a paraphrase, (c) whether the FULL test suite ran or only a targeted subset; if a
subset, say so explicitly.

**3. Raw output, never summary.** Every claim of "fixed"/"passing"/"aligned" shows
raw output (git diff, pytest summary line, actual query rows), not assertion.
"NOT FOUND" is a complete answer. Don't know → say "unconfirmed". A RED test that
goes GREEN before the fix is applied means the test is wrong, not the code right.
(pytest from a reused kernel can be stale — final confirmation via fresh subprocess.)

**4. Show diff → approval → apply, for ANY code change.** Even a one-line patch.
Plan → show exact diff → explicit approval → apply → test. "It's small and obviously
right" is never an exception. The diff must be visible in the chat the operator
reads — terminal scrollback he can't see does not count. Gates are approved
individually unless the operator says "proceed through all".

**5. Stop after 2 tool failures on the same action.** Report the EXACT error and
ask for a different approach. Don't silently defer or loop. (Editing corollary:
3 failed edit attempts on one file = stop and show what's going wrong.)

**6. Clean junk memory the same session it's noticed.** Any persistent-memory or
skill entry that is junk, an error artifact, or a failed write gets removed that
session.

**7. Root cause over patch.** Find why, don't just make the symptom go away.

**8. Pre-execute snapshot for ANY mutating action** (migration, repair script,
direct DB write): dump the rows it will touch FIRST, save to repo root, THEN get
explicit human GO-AHEAD before the real write. Never modify/reset production DB
rows outside this protocol. One-row pinned UPDATEs with WHERE guards only.

**9. Full test suite at the end, not subsets mid-iteration.** Run `pytest tests/`
on **Python 3.10** (this repo's target — NOT the agent host's 3.11) exactly once at
the end. Name the exact commit hash in every report.

**10. Engine entry = `engine/run_engine.py`** (NOT `runner/__init__.py` — no
`__main__`). Single-instance guard via SocketLock **port 19888**. Cooperative stop
via `shutdown_control.py` (stop file + port + PID).

**11. Restart only from a single fully-tested unified branch.** Name the exact
commit hash in every report. Never sign-flip hacks.

**12. Per-bot open_qty ≠ pair-level net.** The exchange sees only the NETTED
position across all bots on a pair (RULE #0). Always compute the signed sum.

**13. Doc updates ship with code changes.** If you change behavior, update the docs
in the same commit. One concern per commit — never bundle fix + data change + docs.

---

## Non-Negotiable Rules (1–13, binding — tighten and extend Standing Rules 1–13)

**1. Approval Gate:** Only a standalone "approved" message authorizes ANY write (edits, git add/commit/stash/checkout/reset, DB writes, scripts, tests). Analysis turns contain ZERO writes. If unsure, treat as write. End turn at "awaiting approval".

**2. Live DB Isolation:** Never open crypto_bot.db except `sqlite3 file:...?mode=ro`. Never run pytest or import engine.* against the main repo or live DB. Snapshot first.

**3. Raw Evidence Only:** Paste tool output verbatim. Never retype, turn into a table, or write "already pasted above". Re-paste the exact block or write "not done".

**4. No Fabricated Output:** Never present unrun output. Label anything reconstructed or illustrative as such.

**5. No Memory Claims on External Systems:** Never state facts about Binance, ccxt, or any library from memory. Cite a code line (`git show HEAD:path` + sed line) or a captured sample, or write "unverified".

**6. Source Labeling:** Label every source: HEAD (`git show HEAD:path`), working tree, or worktree. Never call a working-tree paste "HEAD".

**7. Hash/Number Integrity:** If numbers or hashes differ between turns, say so and stop. Never claim they are equal or adjust one to fit.

**8. "Fixed" Definition:** "Fixed" means: fails before, passes after, output shown. A `side=` kwarg is not proof the value is populated; trace where it comes from.

**9. Complete Sweep:** After "fixed N instances": repo-wide AST + grep including aliases, wrappers, and kwargs; one file:line per hit from a single command at a stated commit; list every excluded site with its reason.

**10. Scope Discipline:** Change only what was approved. Deferred or design-only sites are off-limits. No new behavior (such as a new `fetch_order` call) inside "wiring" fixes and no unrelated edits.

**11. Correction Protocol:** When corrected: one line of acknowledgment, re-run a command to verify, fix it everywhere. Do not "retract" by asserting the opposite without new evidence.

**12. Pre-Send Audit:** Before sending: list your claims; for each, name the raw block that supports it; delete any claim without one.

**13. Engine Startup Code = Write:** Any command that could execute engine startup code (`init_db`, `heal_zombie_bots`, `auto_create_hedge`, `backup_database`, `reconstruct_offline_fills`, `engine.run_engine`) is a write and needs "approved", however small the script looks. Plain-script runs (e.g., `python tests/test_all_functions.py`) that import `engine.database` or call `init_db()` count — they open the live DB, run migrations, create backups, and mutate state.

---

## Safety boundaries (never crossed without explicit operator sign-off)

1. Never modify/delete/reset rows in `crypto_bot.db` or any production DB outside
   the Rule-8 snapshot protocol.
2. Never run a script that places/cancels/modifies a real exchange order outside
   the tested engine code path. Demo/testnet included.
3. Never bypass/weaken/disable the pre-placement guard, test-DB isolation guard,
   or any safety gate.
4. Never restart or kill the engine mid-session without saying so first.
5. Never claim "fixed/verified/aligned" without the raw output that proves it.

## One-shot patch discipline (2026-09-14 postmortem)

Never edit any file over ~100 lines via generated string-patch/rewrite scripts or
repeated fuzzy patches. Write the complete target diff → show it in full → apply
in ONE shot from pre-verified content (each old-block matches exactly once;
`ast.parse` on result) → `py_compile` + tests immediately after. History: ~50
incremental string-surgeries on `engine/health.py` produced 127 junk scripts, 20
backups, a syntactically broken tree, zero result in 2+ hours; the same change
landed cleanly in one reviewed patch. Full account:
`docs/bugs/POSTMORTEM_HEALTH_WHACKAMOLE_20260914.md`.

## Repo hygiene

- No scratch/debug/patch scripts in the repo tree — they live in `$LOCALAPPDATA/Temp`.
- The operator re-verifies reports himself; honest retraction > confident assertion.

---

## Architecture quick reference (full detail: CODEBASE_GUIDE.md)

- **Three-Layer State Model:** (1) Exchange Physical Position = truth for
  *reality*; (2) Bot Orders Ledger (`bot_orders`) = truth for *history*;
  (3) Trades Cache (`trades`) = eventually consistent, computed from the ledger
  via `seal_trade_state()`. `exchange_fills` = immutable append-only fill log,
  the position-computation source (Phase 4+).
- **Proof-Only Reconciliation:** virtual positions derive ONLY from `bot_orders`
  fills via `credit_fill()`. Parity is exact in quantity space, tolerance 0.002.
  UI HEALTHY only when `audit_pair_ledger_vs_exchange()` returns zero rows. No
  raw-SQL heals, no invented fills (`ALLOW_FORENSIC_ADOPT=False`).
- **WriteQueue (INV-31):** ALL writes to `trades`/`bot_orders` go through the
  single-threaded `WriteQueue` singleton. Direct `conn.execute/commit` for
  mutations is prohibited (exception: nested calls inside an active transaction
  block bypass to avoid nested-transaction lockups).
- **Hedge feature = bot-level risk management** (parent/child bot pairs), NOT
  exchange Hedge Mode. Hedge children share the single one-way exchange
  position (ADR-011).
- **Martingale sizing with Kelly/Vol may reduce size, never raise above ladder.**
- Key components: `parity_gates.py` (cycle reset / entry / maintain gates,
  startup repair), `StateReconciler` (heal gates only, escalates to
  REQUIRE_MANUAL_PROOF instead of guessing), GTR (every 10 cycles),
  `shutdown_control.py` (port 19888).

## Domain facts you need before coding

- **`exchange_fills` is immutable and append-only** — position truth.
  `bot_orders` may contain phantom/zero-fill rows; `trades.open_qty` is a cached
  accumulator. Full history = `cycle_floor=0`.
- **Dedup shape:** `UNIQUE(exchange_order_id, fill_ts, qty, price)` — identical
  replays silently ignored; partials with different qty are NOT deduped.
- **`fill_claims` PK `(bot_id, order_id)`** blocks same-key replay credits;
  lookup accepts order_id OR client_order_id — WS-oid + catchup-CID pairs on
  exit-type orders were the exposed double-count path (fixed: the
  `delta<=0 and is_cumulative` dual-write guard, commit 3c5a097).
- **Two-tier health** (`engine/health.py`): tier-1 drift = cycle_floor→now net vs
  exchange; tier-2 `ledger_imbalance` = full-history net vs exchange physical;
  escalates system_status to MISMATCH. Whitelisted migration-era orphans
  (BNB/SOL/SUI/XAU) may legitimately trip tier-2 on first start — expected, not
  a bug.
- **`compute_bot_position(conn=None)` binds to the live DB** via
  `get_connection()`, not the caller's `db_path` — always pass an explicit
  connection in tests.

## Skills that auto-load on the operator's machine (triggers)

- `crypto-bot-agent-discipline` — editing bot code, reporting a fix, "show me raw"
- `safe-trading-bot-ops` — any money-moving action, reading DB/state, repair/heal
- `safe-change-discipline` — about to run a mutating script, declaring system green
- `investigation-discipline` — systematic debugging / root-cause tasks
- `session-patterns-20260920` — 2026-09-20 retrospective four patterns with standing rules
- `crypto-bot-db-verification` — verification patterns for crypto bot DB state
- `trading-bot-attribution-patterns` — forensic attribution gates for multi-bot trading
- `systematic-debugging` — 4-phase root cause debugging: understand bugs before fixing
- `verification-rigor` — operating discipline for reporting work as done — show raw

## Open items (do not silently "resolve")

1. `test_gate_blocks_when_require_manual_proof` — fails identically at clean HEAD; pre-existing, needs root-cause (unverified).
2. 8 P1/P2 anomalies in PROJECT_STATUS.md (XAU ORDER-SYNC loop, stale-cycle_id dedup wedge, INV30 double-count, hedge-child is_active check, audit_bot_wipes signature, GTR lock display, retry-queue false alarm, flatten price=0.0) — OPEN.
3. First production engine start after 2026-09-14 exercises the two-tier health path for the first time.

## Resolved (historical)
- side= caller-wiring: RESOLVED 2026-09-15 — `credit_fill()` HAS `side=` (ledger.py:243/274, commit 3c5a097); 7 of 8 call sites pass the real exchange side. ENTRY-ANCHOR 2632 stays on inference by design. Verify: tests/test_explicit_side_wiring.py.

## For external review agents

Start at `docs/EXTERNAL_REVIEW_GUIDE.md`. Priorities: audit `e00c5ce` (two-tier
health), audit `3c5a097` (dual-write guard), the 8 open anomalies, the test-DB
isolation boundary. Credentials in `.env` — no orders, no DB writes, no pushes
without explicit operator instruction. Report with raw evidence (Rule 3).