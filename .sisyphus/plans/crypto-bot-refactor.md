# Crypto Quant Bot Refactor Plan

This plan covers critical bug fixes, code cleanup, quantitative enhancements, and UI/UX improvements for the Crypto_Quant_Bot project.

## Phase 1: Critical Bug Fixes & Safety
- [x] Fix Partial Fill Over-Allocation Bug in `engine/bot_executor.py` (Line 638)
- [x] Implement Unrealized PnL check for Daily Loss Limit in `engine/risk_manager.py`
- [ ] Replace bare `except:` and `except Exception: pass` blocks with specific exception handling in `engine/runner.py` and `engine/bot_executor.py`
- [ ] Fix duplicate `CREATE TABLE bots` in `engine/database.py` (Lines 83-97)

## Phase 2: Code Cleanup & Deduplication
- [ ] Delete dead/obsolete files: `engine/database_v2.py`, `engine/sync.py`, `engine/sync_v2.py`, `engine/assign_recovery.py`
- [ ] Merge `engine/reconciler.py` and `engine/reconciliation.py` into a single, cohesive reconciliation module
- [ ] Consolidate root-level diagnostic scripts (`check_*.py`, `debug_*.py`, etc.) into `tools/diagnostic.py` with a CLI interface

## Phase 3: Quantitative & Strategy Enhancements
- [ ] Implement actual `correlation_check` logic in `engine/strategies/martingale_strategy.py` (Line 806)
- [ ] Add √n ATR Scaling option for deep grid steps (Step 4+) to account for time-risk expansion
- [ ] Synchronize and document fee/slippage constants across the system (confirming 0.15% total)

## Phase 4: UI/UX Improvements
- [ ] Implement navigation "bridge" from Bot Creator to Monitor (session-state based deep link)
- [ ] Add Dark Mode ("Midnight Terminal") theme option to `ui/app.py`
- [ ] Refactor sidebar to move API Configuration to a dedicated Settings page/modal
- [ ] Promote Risk Heatmap to a primary dashboard component in `ui/views/monitor.py`

## Phase 5: Verification & QA
- [ ] Run full test suite (`pytest`) and verify zero regressions
- [ ] Perform project-level `lsp_diagnostics` check
- [ ] Verify build and startup flow
