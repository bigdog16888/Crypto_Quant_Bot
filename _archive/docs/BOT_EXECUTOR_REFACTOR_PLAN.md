# Bot Executor Refactoring Plan

## Overview
**File:** `engine/bot_executor.py` (1102 lines)
**Status:** Working - Refactor only during maintenance window

## Current Structure
```
BotExecutor (Single monolithic class)
├── process_bot() - Main entry point
├── execute_mission() - Core mission logic
├── manage_pending_entry() - Entry order management
├── process_market_maker() - Market maker strategy
├── _check_order_limits() - Validation
├── _record_order() - Order tracking
├── _execute_limit_with_chase() - Order execution with chase logic
├── execute_entry() - Entry order placement
├── _finalize_entry() - Post-entry processing
├── _simulate_dry_run_entry() - Testing support
├── reconcile_orders() - Order state reconciliation
└── verify_state_sync() - State validation
```

## Recommended Split

### Module 1: `engine/executor/entry_manager.py` (~300 lines)
**Responsibility:** Order entry and execution
```
├── execute_mission()
├── manage_pending_entry()
├── _execute_limit_with_chase()
├── execute_entry()
├── _finalize_entry()
└── _simulate_dry_run_entry()
```

### Module 2: `engine/executor/order_manager.py` (~200 lines)
**Responsibility:** Order tracking and reconciliation
```
├── _check_order_limits()
├── _record_order()
├── reconcile_orders()
└── verify_state_sync()
```

### Module 3: `engine/executor/market_maker.py` (~150 lines)
**Responsibility:** Market maker specific logic
```
└── process_market_maker()
```

### Module 4: `engine/executor/bot_executor.py` (~300 lines)
**Responsibility:** Orchestration and glue code
```
├── BotExecutor class
├── __init__()
└── process_bot()
```

## Migration Steps (Safe)

### Phase 1: Create new modules without changing imports
1. Create `engine/executor/__init__.py`
2. Create `engine/executor/entry_manager.py` (copy methods)
3. Create `engine/executor/order_manager.py` (copy methods)
4. Create `engine/executor/market_maker.py` (copy methods)

### Phase 2: Update imports in bot_executor.py
```python
# OLD
def execute_mission(self, mission, exchange=None, ...):
    ...

# NEW
def execute_mission(self, mission, exchange=None, ...):
    return self.entry_manager.execute_mission(...)
```

### Phase 3: Test thoroughly
- Run all existing tests
- Execute dry trades on testnet
- Monitor for 24 hours

### Phase 4: Final cutover
- Delete extracted methods from bot_executor.py
- Update all import statements

## Benefits
1. **Maintainability** - Smaller files, focused responsibility
2. **Testability** - Each module can be tested in isolation
3. **Reusability** - Modules can be used by other systems
4. **Readability** - Clear separation of concerns

## Risks
1. **Regression** - Moving code introduces bugs
2. **Import cycles** - May create circular dependencies
3. **State sharing** - Methods share `self` state across modules

## Recommendation
**DEFER** - The current implementation works. Only refactor during:
- Planned maintenance window
- After adding comprehensive test suite
- When adding major new features

---
Generated: 2026-01-30
Status: PLANNED (Not implemented)
