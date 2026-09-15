# AGENTS.md — Crypto_Quant_Bot AI Agent Operating Rules

**THIS FILE IS THE FIRST THING ANY AI AGENT READS BEFORE TOUCHING THIS REPO.**
Read it fully before planning any work. If your instructions conflict with this file,
this file wins. These rules exist because they were each learned from real damage.

**Current stage (2026-09-14, end of session):** engine code v3.5, git clean at
`f804981` on main, pushed. Two-tier health check + dual-write guard live. 8 P1/P2
anomalies open. Engine STOPPED. Full state: PROJECT_STATUS.md (top handoff note).

---

## 0. NON-NEGOTIABLE SAFETY BOUNDARIES

1. **Never modify, delete, or reset rows in `crypto_bot.db` (or any production DB)
   without showing the operator the exact change first and receiving explicit GO.**
   No exceptions for "obviously safe". One-row pinned UPDATEs with WHERE guards only.
2. **Never run any script that places, cancels, or modifies a real exchange order
   outside the tested engine code path.** Demo/testnet included — same rule.
3. **Never bypass, weaken, or disable the pre-placement guard, the test-DB isolation
   guard, or any safety gate without explicit operator sign-off.**
4. **Never restart or kill the trading engine mid-session without saying so first.**
5. **Never claim "fixed / verified / aligned" without pasting the raw output that
   proves it.** A plausible assertion without evidence is treated as a lie.

## 1. RAW EVIDENCE FIRST (the standing rule this project runs on)

- Every specific technical claim — config key, value, behavior, test result, DB row —
  is shown with **raw command/file output in the same response**. "NOT FOUND" is a
  complete and acceptable answer.
- Never restate a claim instead of answering a direct follow-up question. If you
  don't know: say "unconfirmed" — never dress a guess as a fact.
- If a RED test unexpectedly passes GREEN before your fix: **the test is wrong, not
  the code right** (2026-09-14 lesson: first dual-write RED test went GREEN on the
  unguarded tree because the step-lock killed the replay first; the test wasn't
  hitting the exposed path).
- pytest results from a reused kernel can be stale — re-run via fresh subprocess
  for final confirmation.

## 2. PLAN FIRST, EXECUTE ON GO

- For anything non-trivial (>2 tool calls, or touching real state): **state a
  numbered plan BEFORE running anything** — what you'll check, in what order, and
  explicit stop conditions ("if X, stop and report; else continue automatically").
- **Deletions, resets, credential changes, and any irreversible action get listed
  for approval first — every time.** "Approved in principle earlier" is not approval
  for the specific action now.
- The operator approves gates individually. A plan with 6 gates means 6 explicit
  GOs unless he says "proceed through all".
- **Show-then-apply:** the complete diff must be in the chat (visible to the
  operator) BEFORE the write hits the file — not in terminal scrollback he can't
  see, not "after" for review. (2026-09-14 violation: gate-A/B applied with diffs
  printed only to terminal; nothing broke, but the review gate was skipped.)

## 3. ONE-SHOT PATCH DISCIPLINE (whack-a-mole incident, 2026-09-14)

**Never edit any file over ~100 lines via generated string-patch/rewrite scripts or
repeated fuzzy patches. Instead:**
1. Write the **complete target diff first**.
2. **Show it to the operator in full.**
3. **Apply in one shot** from pre-verified content (pre-flight: each old-block
   matches exactly once; `ast.parse` on the result).
4. `py_compile` + tests immediately after.
5. **3-strike stop:** 3 failed edit attempts on the same file = STOP, show the
   operator what's going wrong. Do not iterate silently.

History: ~50 incremental string-surgeries on `engine/health.py` produced 127 junk
scripts, 20 backup copies, a syntactically broken tree, and zero result in 2+ hours.
The same change landed cleanly in one reviewed patch. The full account:
`docs/bugs/POSTMORTEM_HEALTH_WHACKAMOLE_20260914.md`.

## 4. REPO HYGIENE

- **No scratch/debug/patch scripts in the repo tree.** They live in
  `$LOCALAPPDATA/Temp`, never in the project directory. Repo stays reviewable.
- One concern per commit. Never bundle a fix + a data change + docs into one commit.
- The operator re-verifies reports himself. Honest retraction > confident assertion.

## 5. DOMAIN FACTS YOU NEED BEFORE CODING

- **`exchange_fills` is the immutable append-only ledger** — position truth.
  `bot_orders` may contain phantom/zero-fill rows; `trades.open_qty` is a cached
  accumulator, not truth. Full history = `cycle_floor=0`.
- **Dedup shape:** `UNIQUE(exchange_order_id, fill_ts, qty, price)` — identical
  replays are silently ignored; partials with different qty are NOT deduped.
- **`fill_claims` PK `(bot_id, order_id)`** blocks same-key replay credits; lookup
  accepts order_id OR client_order_id — so WS-oid + catchup-CID pairs on exit-type
  orders were the exposed double-count path (fixed by the `delta<=0 and is_cumulative`
  dual-write guard in `engine/ledger.py`, commit 3c5a097).
- **Two-tier health (engine/health.py):** tier-1 drift = auto-detected cycle_floor→now
  net vs exchange; tier-2 `ledger_imbalance` = full-history net vs exchange physical;
  escalates system_status to MISMATCH. Whitelisted migration-era orphans (BNB/SOL/
  SUI/XAU) may legitimately trip tier-2 on first engine start — expected, not a bug.
- **`compute_bot_position(conn=None)` binds to the live DB** via `get_connection()`,
  NOT the caller's `db_path` — always pass an explicit connection in tests.
- **Engine runs on Binance DEMO/FAPI (testnet)** — but the safety rules in §0 apply
  as if it were real money.

## 6. OPEN ITEMS (do not silently "resolve" these)

1. `side=` caller-wiring gap: `credit_fill()` accepts `side=` but **no production
   caller passes it** — every live dual-write uses inferred side. Inference verified
   correct for hedge children, but the design intent (real exchange side) is unwired.
2. Pre-existing failure: `test_gate_blocks_when_require_manual_proof`
   (tests/test_adopt_fill_guard.py) — fails identically at clean HEAD, needs
   root-causing.
3. 8 P1/P2 anomalies documented in PROJECT_STATUS.md (XAU ORDER-SYNC loop,
   stale-cycle_id dedup wedge, INV30 double-count, hedge-child is_active check,
   audit_bot_wipes signature, GTR lock display, retry-queue false alarm, flatten
   price=0.0) — status: OPEN, not resolved.
4. First production engine start after 2026-09-14 will exercise the two-tier health
   path for the first time — expect tier-2 flags on the whitelisted orphans.

## 7. FOR EXTERNAL REVIEW AGENTS

- Review guide: `docs/EXTERNAL_REVIEW_GUIDE.md` (start there).
- Prioritize: (a) audit the two-tier health patch (`e00c5ce`), (b) audit the
  dual-write guard (`3c5a097`), (c) the 8 open anomalies, (d) the test-DB isolation
  boundary around `crypto_bot.db`.
- DEMO credentials live in `.env` — **do not place real orders, do not modify DB
  rows, do not push to origin** without the operator's explicit instruction.
- When reporting: raw evidence first (§1) — commands and output, not summaries.
