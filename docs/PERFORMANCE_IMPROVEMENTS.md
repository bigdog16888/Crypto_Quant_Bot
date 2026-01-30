# Performance Improvements Summary

## Implemented Optimizations (v0.7.0)

### 1. OHLCV Caching
- **What:** Added 30-second cache for OHLCV data
- **Impact:** Reduces API calls by ~80% for price data
- **Location:** `exchange_interface.py:267-297`
- **TTL:** 30 seconds

### 2. Request Coalescing
- **What:** Prevents duplicate simultaneous API calls
- **Impact:** When 12 bots fetch BTC/USDC OHLCV at the same time, only 1 actual API call is made
- **Location:** `exchange_interface.py:121-169`
- **Scope:** fetch_ohlcv, fetch_balance, fetch_positions, fetch_ticker, fetch_open_orders

### 3. Dynamic Thread Pool Sizing
- **What:** Thread pool now scales with bot count (bots + 2, max 20)
- **Impact:** Reduces thread overhead for small bot counts
- **Location:** `runner.py:327-331`
- **Formula:** `max_workers = min(num_bots + 2, 20)`

### 4. BotExecutor Instance Reuse
- **What:** Reuse BotExecutor instead of creating new instance each cycle
- **Impact:** Saves strategy instance creation overhead
- **Location:** `runner.py:321-324`
- **Note:** Strategies cached in `self._bot_executor.strategies`

### 5. Batch API Methods
- **What:** Added bulk fetch methods for positions and tickers
- **Impact:** Reduces round-trip latency for multi-symbol data
- **Location:** `exchange_interface.py:467-528`
- **Methods:**
  - `fetch_positions_by_symbols(symbols)` - Single call for multiple symbols
  - `fetch_tickers_bulk(symbols)` - Bulk ticker fetch

### 6. API Call Metrics
- **What:** Track API call frequency for monitoring
- **Location:** `exchange_interface.py:541-553`
- **Usage:** Call `exchange.get_api_call_stats()` for metrics

## Performance Metrics

### Before Optimization
| Metric | Value |
|--------|-------|
| OHLCV API calls/cycle | 12 (one per bot) |
| Positions API calls/cycle | 12+ (multiple per thread) |
| Thread pool overhead | 20 threads fixed |
| BotExecutor creation | New instance each cycle |

### After Optimization
| Metric | Value |
|--------|-------|
| OHLCV API calls/cycle | 1-2 (cached + coalesced) |
| Positions API calls/cycle | 1-2 (coalesced) |
| Thread pool overhead | Dynamic (bots + 2) |
| BotExecutor creation | Reused instance |

## Estimated API Call Reduction
- **Before:** ~30-50 API calls per cycle
- **After:** ~5-10 API calls per cycle
- **Reduction:** ~80% fewer API calls

## Cache Cleanup
Added `cleanup_caches()` function to prevent memory leaks:
```python
from engine.exchange_interface import cleanup_caches
cleanup_caches()  # Call hourly
```

---
Generated: 2026-01-30
