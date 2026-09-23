# HANDOFF — Crypto_Quant_Bot

**Window: NIGHT 2 (2026-09-23 → 09-24) | Engine: STOPPED (untouched) | Git HEAD: `953de15` (63 ahead of origin/main)**

---

## TL;DR — What happened this window (NIGHT 2 autonomous mission)

**2 of 3 tasks complete; Task 2 blocked on a disproven premise (evidence in `OVERNIGHT_REPORT_NIGHT2.md` §3).**

| # | Work | Commit / Action | Status |
|---|------|-----------------|--------|
| Task 1 | UI two-tier status: ribbon = tier-1 live parity (`🟢 HEALTHY`), tier-2 dust → `ℹ️ LEDGER ARCHIVE ADVISORY` caption | `9ba4cad` | ✅ Done |
| Task 3 | GTR lock indicator in Live Monitor header (`🟢 ACTIVE` / `🔴 LOCKED` / `⚪ DISENGAGED`) | `953de15` | ✅ Done |
| Task 2 | Archive migration-era phantom `bot_orders` | — | 🛑 **BLOCKED — premise wrong** |

**Why Task 2 is blocked (proof chain, all read-only):**
1. Tier-2 `ledger_imbalance` is computed from **`exchange_fills`** — `health.py:298-301` → `compute_bot_position()` → `position_ledger.py:94-98` (`FROM exchange_fills`). The `bot_has_fills` gate itself counts `exchange_fills` (`health.py:218-221`).
2. **Zero unowned `bot_orders` rows exist** (`bot_id IS NULL` = 0; `bot_id` not in `bots` = 0).
3. Tier-2 values reproduce **exactly** from full-history `exchange_fills` side-signed sums (ETH +8.097, LINK +454.84, XAU +0.105) — 0 duplicate fill groups.
4. ⇒ Archiving `bot_orders` changes the tier-2 number by **nothing**. Zeroing it would require mutating the immutable `exchange_fills` log — forbidden (AGENTS.md safety #1) and wrong (residue is real closed history).
5. **Correct fix (designed, NOT applied):** dormant-bots exclusion gate in `engine/health.py` — when ALL bots on a pair are `is_active=0` AND exchange physical=0, report residue as informational, not `ledger_imbalance`. Active-bot pairs keep strict tier-2. Needs diff + approval.

**New finding (not in task list):** SUIUSDC is a health blind spot — all 3 SUI bots are `is_active=0` + `open_qty=0`, so `health.py:165-168` excludes the pair from the tier-2 scan entirely despite 80+ fill rows (side-signed sum ≈ +335.9, exchange flat). Additionally forward-test grid order **`185035956` (11.8 SUI BUY) is `filled` on exchange** but has **no `exchange_fills` row** and a stale `open` `bot_orders` row. TP `185035955` already `canceled` on exchange. **Exchange flat, no resting orders, no live risk** — but the ledger over-states SUI history and the health check can never see it.

**Test suite: 703 passed, 0 failed, 2 skipped, 0 ERRORS** (fresh run at `953de15`, covers both UI commits).

---

## Current Safety State

| Item | Status |
|------|--------|
| Engine | **STOPPED** all night — explicit operator go-ahead required |
| Live positions | **NONE** — full `fetch_positions()` scan returned zero non-zero positions |
| Resting orders | **NONE** verified for SUI (TP canceled, grid filled+closed); no placements made all night |
| Tier-1 parity | **HEALTHY** — worst_gap_usd = 0.0, 0 mismatched pairs |
| Tier-2 | **LEDGER_ADVISORY** — 5 dormant pairs (LINK +454.84, SOL +52.81, ETH +8.097, BTC +0.02, XAU +0.105); now surfaced as amber advisory, not red MISMATCH |
| DB writes this night | **ZERO** — all queries `mode=ro` |
| Orders placed/canceled | **NONE** |
| Safety gates | Untouched |
| UI | streamlit at localhost:8501, read-only, live-verified via `scripts/tools/inspect_live_ui.py` |

---

## Open Items (next session starts here)

### 1. Tier-2 dormant-bots exclusion gate (Task 2's correct remedy) — P2
- `engine/health.py`: all-bots-`is_active=0` + physical=0 ⇒ informational, not `ledger_imbalance`.
- Needs: diff → approval → apply → full suite. **Not started (no code written).**

### 2. SUI blind spot + uncredited fill `185035956` — P2, decision needed
- (a) Fold all-dormant pairs into the tier-2 gate fix (item 1) — surfaces SUI residue as advisory.
- (b) Reconcile the missing 11.8 SUI fill via the tested forensic path (reconciler / offline-fill reconstruction) and refresh the stale `bot_orders` status.
- Both touch DB/code → Rule-8 snapshot + approval required.

### 3. Finding 2 — Side inference gap in parity_gates/database.py (P1, pre-existing)
- `parity_gates.py:1135` + `database.py:2173` call `credit_fill()` without `side=`. Needs diff + test + approval. **Not started.**

### 4. Repo hygiene — P3
- ~34 untracked scratch files at repo root (`check_*.py`, `debug_*.py`, …) → `%LOCALAPPDATA%\Temp`. **Deletion needs approval.**
- `AGENTS.md` working tree has a stale stage header (docs-only, uncommitted).

### 5. Push decision
- 63 commits ahead of origin/main, not pushed. Operator call.

---

## Key Files Modified This Window

```
engine/health.py            # tier1_status/tier2_status split, no tier-2→MISMATCH escalation (9ba4cad)
ui/views/monitor.py         # ribbon=tier1, LEDGER ARCHIVE ADVISORY caption, GTR lock indicator (9ba4cad, 953de15)
OVERNIGHT_REPORT_NIGHT2.md  # full overnight deliverable (new, untracked)
PROJECT_STATUS.md           # updated to NIGHT 2 state
HANDOFF.md                  # this file
```

---

## Commands for Next Session

```bash
cd D:/Crypto_Quant_Bot
git log --oneline -5        # expect 953de15 at top, 63 ahead
git status --short          # AGENTS.md modified; OVERNIGHT_REPORT_NIGHT2.md + scratch files untracked

# Live UI ground truth (after any UI/health edit — mandatory):
python scripts/tools/inspect_live_ui.py
# expect in DOM: "🟢 HEALTHY" ribbon, "ℹ️ LEDGER ARCHIVE ADVISORY", "🛡️ GTR Lock: ⚪ DISENGAGED"

# Tier-2 source verification (read-only):
python -c "
import sys; sys.path.insert(0,'.')
from config.settings import config
from engine.exchange_interface import ExchangeInterface
from engine.health import get_system_health as g
ex = ExchangeInterface(config.MARKET_TYPE)
h = g(db_path=config.PATHS['DB_FILE'], exchange_instance=ex, norm_fn=lambda s: __import__('engine.exchange_interface', fromlist=['normalize_symbol']).normalize_symbol(s), qty_tolerance_fn=lambda: 0.002, force_refresh=True)
print('tier1:', h['tier1_status'], 'tier2:', h['tier2_status'])
"

# Full suite (Python 3.10 target per Rule 9; 3.11 used this night — 703/703 green):
python -m pytest tests/ --ignore=tests/test_playwright_ui.py -q --no-header
# expect: 703 passed, 2 skipped
```

---

## Non-Negotiable Rules (from AGENTS.md, re-stated for next agent)

1. **NO engine start** without explicit operator go-ahead
2. **NO DB writes** without Rule-8 snapshot + approval (`exchange_fills` is immutable — never)
3. **NO "fixed" claim** without raw output (git diff, pytest, query rows, live DOM)
4. **Show diff → approval → apply → test** for every code change
5. **Stop after 2 tool failures** on same action — report exact error, change approach
6. **Item-by-item** — complete each numbered item, STOP, wait for approval before next
7. **Raw or nothing** — paste tool output verbatim; never retype, tableize, or say "already pasted"
8. **Evidence over summary** — every claim needs raw output shown
9. **Root cause over patch** — Task 2's premise failed exactly this way; verify the data path before building the remedy
10. **Show before you act** — deletions, resets, credential changes get approval first
11. **Say what you don't know** — "unconfirmed" beats a confident guess
12. **Scope discipline** — change only what was approved
13. **Engine startup = write** — any command that could run init_db/heal/backup needs approval

---

## Context Recovery

If context is lost, recover with:
- `session_search(query='NIGHT 2 tier-2 exchange_fills premise disproven', session_id='20260923_114638_a1ee0b')`
- `session_search(query='9ba4cad tier-1 tier-2 ribbon LEDGER ADVISORY', session_id='20260923_114638_a1ee0b')`
- `session_search(query='953de15 GTR lock indicator DISENGAGED', session_id='20260923_114638_a1ee0b')`
- `session_search(query='185035956 SUI uncredited fill blind spot', session_id='20260923_114638_a1ee0b')`
- `session_search(query='20260923 phase3 phase4 complete soak test', session_id='20260922_102457_f7f30b')`

---

**End of NIGHT 2 handoff. Engine stopped, exchange flat, zero DB writes, 703 green. Next: tier-2 dormant-bots gate diff → approval; SUI decision; Finding 2 side= wiring.**
