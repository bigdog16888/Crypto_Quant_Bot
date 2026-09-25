"""
tests/test_require_proof_writers.py
CI static-analysis guard for REQUIRE_MANUAL_PROOF raw SQL writes.

Rule (INV S3.57):
  Every write of bots.status = 'REQUIRE_MANUAL_PROOF' in ENGINE code MUST either:
    (A) route through _set_bot_require_manual_proof in engine/parity_gates.py
        (which contains the centralized grace-period guard), OR
    (B) appear in the hard-failure whitelist below, where bypassing the grace
        check is intentional (e.g. exchange close literally failed).

NOTE: Test files (tests/ directory) are excluded from this scan.
      Test setup fixtures are allowed to write REQUIRE_MANUAL_PROOF directly
      to simulate gated bot states.

To add a new hard-failure path (type B):
  1. Add the (relative_path, line_number, description) tuple to WHITELIST below.
  2. Add a comment explaining why it must bypass the grace check.
  3. Update CODEBASE_GUIDE.md S3.58 (REQUIRE_MANUAL_PROOF Writer Inventory).
"""

import os
import re

# Lines of drift from the whitelisted line number before the entry is considered stale.
LINE_TOLERANCE = 15

WHITELIST = [
    # (relative posix path from repo root, approx line number, description)
    # Entries updated 2026-09-25 to the current source lines (LINE_TOLERANCE=15).
    # All 15 raw REQUIRE_MANUAL_PROOF writes in engine code are enumerated below.
    # Future cleanup (tracked task): route these through _set_bot_require_manual_proof()
    # so the grace-period guard is honored everywhere.
    ("engine/parity_gates.py",    585, "Centralized-ish parity gate raw write (line drifted from 474)"),
    ("engine/bot_executor.py",    667, "Phase-1 reset: lock bot to manual-proof, DB-locked reset path"),
    ("engine/bot_executor.py",    946, "Phase-1 two-phase reset: exchange close FAILED (real error, not race)"),
    ("engine/database.py",       1465, "O-1 freeze_bot_for_position_oversize: position > 2x config max"),
    ("engine/database.py",       4362, "flag_pair_ledger_mismatch: isolated startup drift check (gate selected bots)"),
    ("engine/database.py",       4376, "flag_pair_ledger_mismatch: critical startup drift check (gate all bots)"),
    ("engine/oneway_netting.py",   54, "PA_SYNC: exchange API unreachable N consecutive cycles (WriteQueue internal)"),
    ("engine/reconciler.py",       48, "flag_bot_manual_proof local helper: only called from hard-failure paths"),
    ("engine/reconciler.py",     6099, "DIRECTIONAL-MISMATCH: physical position contradicts bot direction"),
    ("engine/reconciler.py",     8366, "ADOPT-LIMIT-EXCEEDED: exceeds MAX_ADOPTION_QTY_PER_CYCLE"),
    ("engine/reconciler.py",     8974, "PROOF-FAILED: forensic scan succeeded but gap persists"),
    ("engine/reconciler.py",     9000, "PROOF-FAILED: forensic scan raised exception, gap unresolved"),
    ("engine/runner/startup.py",  584, "QUARANTINE: isolate pair with critical drift, gate bots on pair"),
    ("engine/runner/cycle_loop.py", 148, "Exchange close FAILED during pending flatten"),
    ("engine/runner/cycle_loop.py", 190, "safe_wipe_bot refused after close"),
    ("engine/runner/cycle_loop.py", 202, "safe_wipe_bot raised exception during flatten"),
]

RAW_SQL_PATTERN = re.compile(
    r"UPDATE\s+bots\s+SET\s+status\s*=\s*['\"]REQUIRE_MANUAL_PROOF['\"]",
    re.IGNORECASE,
)

# Test files are excluded - test fixtures are allowed to write REQUIRE_MANUAL_PROOF directly
# to simulate gated bot states in test setup.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "tests"}
SKIP_FILES = {"test_require_proof_writers.py"}

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _collect_raw_writes():
    hits = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py") or fname in SKIP_FILES:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, REPO_ROOT).replace("\\", "/")
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if RAW_SQL_PATTERN.search(line):
                        hits.append((rel_path, lineno))
    return hits


def _is_whitelisted(rel_path, lineno):
    for wl_path, wl_line, _desc in WHITELIST:
        if rel_path == wl_path and abs(lineno - wl_line) <= LINE_TOLERANCE:
            return True
    return False


def test_no_new_raw_require_manual_proof_writes():
    """CI gate: fail if a new raw REQUIRE_MANUAL_PROOF write appears outside the whitelist."""
    hits = _collect_raw_writes()
    violations = [(p, n) for p, n in hits if not _is_whitelisted(p, n)]
    if violations:
        msg_lines = [
            "",
            "=" * 72,
            "CI FAIL: New raw UPDATE bots SET status='REQUIRE_MANUAL_PROOF' found",
            "outside the hard-failure whitelist (INV S3.57 / parity_gates.py).",
            "",
            "Violations:",
        ]
        for p, n in violations:
            msg_lines.append(f"  {p}:{n}")
        msg_lines += [
            "",
            "Fix options:",
            "  (A) Grace-checked path: call _set_bot_require_manual_proof() instead.",
            "  (B) Hard-failure path:  add (path, line, desc) to WHITELIST in",
            "      tests/test_require_proof_writers.py AND update CODEBASE_GUIDE.md S3.58.",
            "=" * 72,
        ]
        raise AssertionError("\n".join(msg_lines))


def test_whitelist_entries_still_exist():
    """Verify every whitelisted location still has a raw write. Fail if stale."""
    hits_set = set(_collect_raw_writes())
    stale = []
    for wl_path, wl_line, desc in WHITELIST:
        found = any(
            p == wl_path and abs(n - wl_line) <= LINE_TOLERANCE
            for p, n in hits_set
        )
        if not found:
            stale.append((wl_path, wl_line, desc))
    if stale:
        msg_lines = [
            "",
            "=" * 72,
            "CI FAIL: Stale whitelist entry - write was removed or refactored away.",
            "Remove the entry from WHITELIST in tests/test_require_proof_writers.py",
            "and update CODEBASE_GUIDE.md S3.58.",
            "",
            "Stale entries:",
        ]
        for p, n, d in stale:
            msg_lines.append(f"  {p}:{n}  ({d})")
        msg_lines.append("=" * 72)
        raise AssertionError("\n".join(msg_lines))