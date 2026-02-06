## 2026-02-05 Bot Executor Findings
- `engine/bot_executor.py` contains TWO definitions of `execute_mission`. The one at 776 is the active one. The one at 291 is dead/duplicate.
- `manage_pending_entry` was failing to retry remaining amount after partial fills.
- Grid maintenance logic was placing FULL qty orders even if a partial fill had already occurred for that step, leading to over-allocation.
- Deterministic IDs `CQB_{bot_id}_GRID_{step}` are the source of truth for tracking.
  
## Partial Fill Over-Allocation Fix (2026-02-05)  
- Fixed `manage_pending_entry` to correctly calculate `remaining_usd = base_size - (filled * avg_price)` and retry with the remainder.  
- Fixed `execute_mission` (maintain_orders) to check for existing partial fills using the deterministic ID before placing new grid orders.  
- Subtracted filled quantity from `grid_qty` if a partial fill was detected on the exchange.  
- Fixed unbound variable `log_trade` by ensuring top-level import is clean and removing conflicting internal imports.  
- Resolved type errors where `g_params` was being treated as a specific typed dictionary. 
  
## Cleanup and Type Fixes (2026-02-05)  
- Removed duplicate `execute_mission` method from `engine/bot_executor.py` (kept the active one at line ~477).  
- Grouped imports from `engine.database` to resolve `log_trade` unbound errors.  
- Fixed type error for `g_params` by explicitly typing it as `Dict[str, Any]` to allow both boolean and string values.  
- Added missing `typing` imports (`Dict`, `Any`, `Union`, `Optional`).  
- Verified that LSP diagnostics were reporting stale errors based on old line numbers. 
## Daily Loss Limit Fix  
- Modified `engine/risk_manager.py` to include Unrealized PnL in the daily loss limit check.  
- Added `get_unrealized_pnl` helper function to fetch positions and sum unrealized PnL.  
- Support for both per-bot and account-wide loss limits.  
- Integrated with `ExchangeInterface` module-level caching for efficiency.  
- Added support for optional `exchange_snapshot` to further reduce API calls if called from `BotExecutor`. 

## State Reconciliation Merger (2026-02-05)
- Merged `engine/reconciliation.py` and `engine/reconciler.py` into a unified `engine/reconciler.py`.
- Unified `StateReconciler` and `DeepReconciler` into a single class with an alias for backward compatibility.
- Integrated "Auto-Healing" (ghost order cleanup) and "Smart Adoption" (orphaned position recovery) into the primary reconciliation engine.
- Added `detect_offline_fills` to handle order fills that occurred while the bot was offline.
- Updated all imports in `runner.py`, test files, and debug scripts to use `engine.reconciler`.
- Cleaned up pre-existing binding and null-safety issues in `engine/runner.py`.
