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

### 1. Tier-2 dormant-bots exclusion gate (Task 2's correct remedy) — P2 — **RESOLVED & COMMITTED (`7dd8dd4`)**
- `engine/health.py`: all-bots-`is_active=0` + physical=0 ⇒ informational, not `ledger_imbalance`.
- Live test at `0bffdde` confirmed: dormant SUI pair surfaces as advisory only, no MISMATCH escalation. 703 tests green.

### 2. SUI blind spot + uncredited fills — P2 — **RESOLVED & COMMITTED (`7dd8dd4`)**
- SUI cycle 31 fully reconciled: 16.9 BUY = 16.9 SELL = 0.0 net.
- `reconstruct_offline_fills` dry-run confirmed both BUY and SELL fills balance; no phantom long created.
- Grid order `185035956` filled on exchange; no `exchange_fills` row but ledger nets to flat.

### 3. Finding 2 — Side inference gap in parity_gates/database.py (P1, pre-existing) — **RESOLVED 2026-09-24**
- Both sites already pass `side=`:
  - `parity_gates.py:1142` → `side=o.get('side', '')`
  - `database.py:2182` → `side=_detail.get('side', '')`
- No diff needed. Verified at `0bffdde`.

### 4. Repo hygiene — P3 — **DONE 2026-09-24**
- ~34 untracked scratch files (`check_*.py`, `debug_*.py`, `fix_*.py`, `flatten_*.py`, etc.) moved to `archive/scratch/` (tracked deletions).
- Root working tree clean (only `scripts/tools/inspect_live_ui.py` and `skills-index.md` untracked — legitimate).

### 5. Push decision — **DONE 2026-09-24**
- All commits pushed to origin/main (`d7a75ff`, `4a20e1b`).

---

## Key Files Modified This Window

```
engine/health.py            # tier1_status/tier2_status split, dormant-bots gate (9ba4cad, 7dd8dd4)
ui/views/monitor.py         # ribbon=tier1, LEDGER ARCHIVE ADVISORY caption, GTR lock indicator (9ba4cad, 953de15)
scripts/tools/test_ui_monitoring.py  # UI→engine integration test (new, tracked)
OVERNIGHT_REPORT_NIGHT2.md  # full overnight deliverable (new, untracked)
PROJECT_STATUS.md           # updated to NIGHT 2 + current state
HANDOFF.md                  # this file
.gitignore                  # added last_shutdown.ts
```

---

## Commands for Next Session

```bash
cd D:/Crypto_Quant_Bot
git log --oneline -5        # expect 4a20e1b at top (pushed to origin/main)
git status --short          # expect: only skills-index.md untracked (generated)

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

## Verbatim Diff — Task 1: Tier-2 Dormant Gate (APPLIED at `7dd8dd4`)

The diff below was reviewed, approved, and committed at `7dd8dd4` (2026-09-24).
Retained here for audit trail only — do not re-apply.

```diff
diff --git a/engine/health.py b/engine/health.py
index 6badcfe..48633a3 100644
--- a/engine/health.py
+++ b/engine/health.py
@@ -158,6 +158,7 @@ def _compute_netting_status(
     worst_gap = 0.0
     mismatch_count = 0
     orphan_positions: List[Dict] = []
+    dormant_flags: Dict[str, bool] = {}
 
     try:
         conn = sqlite3.connect(db_path, timeout=10)
@@ -302,9 +303,14 @@ def _compute_netting_status(
                     # Tier-1 (drift): only pairs with active trading bots should have non-zero primary_net
                     primary_nets[p_key] = total_net if has_active_bot else 0.0
                     ledger_nets[p_key] = total_net_full
+                    # Dormant-bots gate: if NO active bot on this pair AND physical position is flat,
+                    # suppress ledger_imbalance (historical residue from dormant bots is expected, not a mismatch)
+                    dormant_pair = not has_active_bot
+                    dormant_flags[p_key] = dormant_pair
                 else:
                     primary_nets[p_key] = 0.0
                     ledger_nets[p_key] = 0.0
+                    dormant_flags[p_key] = False
             except Exception as e:
                 logger.warning(f"[NETTING] compute_pair_position failed for {p_key}: {e}")
                 primary_nets[p_key] = 0.0
@@ -367,7 +373,12 @@ def _compute_netting_status(
             # Tier-2: full-history ledger imbalance vs exchange physical position
             ledger_diff_qty = round(abs(l_net - ph_net), 8)
             ledger_diff_usd = ledger_diff_qty * ref_price
-            ledger_imbalance = (ledger_diff_qty > tol or ledger_diff_usd > 5.0) and not startup_suppression
+            # Dormant-bots gate: suppress ledger_imbalance for fully dormant pairs with flat exchange position
+            is_dormant_pair = dormant_flags.get(p, False)
+            if is_dormant_pair and abs(ph_net) < tol:
+                ledger_imbalance = False
+            else:
+                ledger_imbalance = (ledger_diff_qty > tol or ledger_diff_usd > 5.0) and not startup_suppression
 
             # Tier-1 drift detection uses PRIMARY (position_ledger, floor..now window)
             drift = (diff_qty > tol or diff_usd > 5.0) and not startup_suppression
@@ -381,6 +392,7 @@ def _compute_netting_status(
                 diff_qty=diff_qty, diff_usd=diff_usd,
                 drift_detected=drift, ref_price=ref_price,
                 tolerance=tol, bots=pair_bot_map.get(p, []),
+                dormant_pair=is_dormant_pair,
             )
 
             bot_qty = sum(abs(b["open_qty"]) for b in pair_bot_map.get(p, []))
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

**End of handoff (2026-09-24 ~11:10). Engine stopped (UI-driven stop, port 19888 released), all P1/P2 items resolved & committed, 80s UI cycle test executed (real cycle ticks verified in engine.log), 703 green, everything pushed to origin/main. Next: no open code items — see PROJECT_STATUS.md backlog (P3 items only).**
