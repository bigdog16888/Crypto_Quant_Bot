# External Review Guide — Crypto_Quant_Bot (for online review agents)

**Date prepared: 2026-09-14 (end of session). Repo: github.com/bigdog16888/Crypto_Quant_Bot**
**Read AGENTS.md first — it contains the operating rules and safety boundaries that
apply to you too. This guide tells you what to look at and what "done" means.**

---

## 1. Project in 60 seconds

Grid-trading bot engine (Python 3.11, SQLite `crypto_bot.db`, Binance DEMO FAPI),
v3.5 one-way hedge architecture (see `TRADING_ARCHITECTURE.md`). Positions are
computed from an **immutable append-only `exchange_fills` ledger** (never from
`bot_orders`, which contains phantom zero-fill rows). Engine currently STOPPED,
working tree clean at `f804981` (main, pushed). Streamlit UI on localhost:8501
when running. 25 test bots + 8 production-ish bots on demo.

## 2. Current stage (verified 2026-09-14)

| What | State |
|---|---|
| Git HEAD | `f804981` on main, pushed to origin |
| Engine | STOPPED (since ~11:57 2026-09-14) |
| Test suite | 642 passed / 21 failed / 11 errors — failures pre-existing baseline (playwright collection error is env-only) |
| Two-tier health | LIVE (commit `e00c5ce`) — tier-2 ledger_imbalance + MISMATCH escalation |
| Dual-write + guard | LIVE (commit `3c5a097`) — `side=` param + `delta<=0 and is_cumulative` guard |
| Open anomalies | 8 P1/P2 (PROJECT_STATUS.md §"8 remaining") + 2 follow-ups below |

### Follow-ups opened tonight (not bugs in shipped code, gaps)
1. **`side=` caller-wiring** — `credit_fill()` accepts it, no caller passes it.
2. **`test_gate_blocks_when_require_manual_proof`** — pre-existing failure at clean HEAD.

## 3. Review priorities (in order)

### P1 — audit tonight's two patches (highest value, freshest)
- **`e00c5ce` (health.py two-tier).** Questions a reviewer should answer:
  - Is `cycle_floor=0, cycle_ceiling=None` genuinely full-history? (Verify against
    `_fetch_fills_for_bot` in `engine/position_ledger.py` — floor is `cycle_id >= ?`.)
  - The explicit-connection change: does `conn_pair` leak on the `else`/exception
    paths? (Check the `finally` block placement.)
  - Is the MISMATCH escalation sound — can tier-2 fire during startup suppression
    and then never re-evaluate? (`startup_suppression` gates `ledger_imbalance`
    itself; suppression window is 120s.)
- **`3c5a097` (ledger.py dual-write + guard).** Questions:
  - Reachability of the double-count path: exit-type orders via WS-oid + catchup-CID
    (different `fill_claims` keys) — is the RED test's scenario faithful to the
    production callers (`bot_executor.py` catchup path)?
  - Guard correctness: `delta <= 0 and is_cumulative` — any path where a legitimate
    NEW fill arrives with `delta<=0` on a cumulative call that we'd now silently drop?
  - Inference branch: `side=` absent → bot direction + order_type. Hedge children
    verified (100324/100316 entries → BUY). What about `flatten_close`, `dust_close`,
    `adoption_reduce` on SHORT bots — SELL/BUY mapping correct?

### P2 — the 8 open anomalies (PROJECT_STATUS.md)
Each has reproduction evidence in the session record; all still OPEN. XAU
ORDER-SYNC credit loop (300× partial fills, credit write swallowed) is the most
consequential. Also check `docs/bugs/CATCHUP_FILL_RACE_ROOT_CAUSE_20260904.md`
for the race this architecture already fixed.

### P3 — architecture-level review
- `TRADING_ARCHITECTURE.md` (v3.5 one-way hedge design, ADRs in `docs/adr/`).
- Safety boundary audit: is the test-DB isolation around `crypto_bot.db` actually
  airtight? (See AGENTS.md §0 — the rules exist because of real incidents.)

## 4. How to run the tests

```
cd D:/Crypto_Quant_Bot    (or your clone)
python -m pytest tests/ -q --ignore=tests/test_playwright_ui.py
python -m pytest tests/test_ledger_imbalance.py tests/test_dual_write_guard.py -v
```
Expected: the two named suites GREEN; the full suite shows the pre-existing
baseline failures (21 failed / 11 errors — byte-identical set documented in
PROJECT_STATUS.md; playwright is env-only).

## 5. Ground rules for reviewers (binding)

From AGENTS.md — repeated here because it matters:
- **Raw evidence first.** Report commands + output, not conclusions.
- **Do not place/cancel/modify orders** (even demo), **do not modify DB rows**,
  **do not push to origin** without the operator's explicit instruction.
- Engine is STOPPED. If you need it running for a test, ask the operator first.
- If you find a bug: report with reproduction steps + evidence. Do not fix-and-push
  on your own initiative; propose the fix as a diff.

## 6. Where everything lives

- `AGENTS.md` — operating rules, safety boundaries, domain facts, open items
- `PROJECT_STATUS.md` — live state snapshot, handoff note, test baseline, 8 anomalies
- `docs/bugs/POSTMORTEM_HEALTH_WHACKAMOLE_20260914.md` — tonight's process incident
- `docs/bugs/CLEANUP_MANIFEST_20260914.md` — what was deleted/kept and why
- `TRADING_ARCHITECTURE.md`, `docs/adr/`, `ARCHITECTURE_DECISIONS.md` — design docs
- `SESSION_HANDOFF_20260913.md` — prior session handoff
- `README.md` — setup
