# ADR-001: Partial Fill Order Retry Logic

## Status
**Accepted** - Implemented 2026-02-05

## Context
When an entry order is partially filled (e.g., 50% of intended size), the retry logic was incorrectly placing another FULL order instead of only the remaining amount. This caused over-allocation (150% position size when only 100% was intended).

## Decision
Calculate `remaining = total_target - filled_amount` and retry with only the remaining amount, not the full order size.

### Before (Bug):
```
Target: $100
Filled: $50 (50%)
Retry: $100 (FULL ORDER)  ❌
Result: $150 total (150% - OVER-ALLOCATED)
```

### After (Fixed):
```
Target: $100
Filled: $50 (50%)
Retry: $50 (remaining)  ✅
Result: $100 total (100% - CORRECT)
```

## Consequences

### Positive
- Eliminates position over-allocation
- More precise order sizing
- Better risk management

### Negative
- Requires tracking filled vs. intended amounts (minor complexity increase)

## Implementation
- Location: `engine/bot_executor.py`
- Function: `manage_pending_entry()`, `execute_mission()`
- Tests: `tests/test_critical_fixes.py::TestPartialFillHandling`

## References
- Related Issue: Position sizing bug causing portfolio imbalance
