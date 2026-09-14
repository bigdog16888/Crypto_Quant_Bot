# Cleanup Manifest — 2026-09-14 Session Scratch Files

## Deleted (Gate 2, operator-approved)

### Root-level throwaway patch/debug scripts (111 files)
Tonight's whack-a-mole scratch: `add_*.py`, `apply_*.py`, `clean_fix.py`,
`complete_fix.py`, `create_fixed_*.py`, `debug_*.py` (29 variants), `do_patch.py`,
`final_*.py`, `fix_*.py` (24 variants), `implement_*.py`, `insert_*.py`,
`manually_fix_*.py`, `move_*.py`, `patch_health.py`, `prepare_health_fix.py`,
`remove_*.py`, `replace_*.py`, `rewrite_*.py`, `step1_*.py`, `step2_*.py`,
`test_bot_processing.py`, `test_dictionary_storage.py`, `test_compute_bot_position.py`,
`test_direct_call.py`, `test_ledger_imbalance_computation.py`, `test_netting_construction.py`,
`test_netting_direct.py`, plus duplicates at successive fix attempts.
Each was a single-purpose read/replace/rewrite script applied to engine/health.py.

### Stray non-script files (2)
- `final_answer.md` — scratch analysis notes
- `cd` — accidental file created by a malformed shell command

### engine/ health.py backup copies (17)
`health.py.backup`, `.backup2`, `.backup3`, `.backup_before_abs`, `.backup_before_debug`,
`.backup_before_final_fix`, `.before_debug`, `.before_fix`, `.before_fix2`,
`.before_floor_fix`, `.debug`, `.debug2`, `.debug3`, `.debug_backup`, `.orig`,
`.original`, `health_debug.py`, `health_modified.py`

### Scratch test variants (2)
- `tests/test_ledger_imbalance_debug.py` (160 lines)
- `tests/test_ledger_imbalance_red.py` (163 lines)
Near-identical drafts of the kept `tests/test_ledger_imbalance.py` (178 lines);
content-verified as duplicates before deletion.

## Deliberately kept (pre-existing, not tonight's mess — operator-approved)
- `scripts/compute_position.py`, `scripts/decision_a_details.py`,
  `scripts/decision_a_info.py`, `scripts/verify_orphans.py` — pre-session project scripts
- `last_shutdown.ts` — engine shutdown marker

## Data fix applied (Gate 5, operator-approved)
- `exchange_fills` row id=2609 (`CQB_10016_GRID_20_4`, BTC/USDC BUY 0.008 @ 78543.3):
  `fill_ts` 1789004056149 (ms-epoch, only such row of 558) → 1789004056 (2026-09-10 09:34:16).
  Verified: rowcount=1 on pinned UPDATE; 0 ms-epoch rows remain; cycle-21 range coherent.
