# CQB Engine Coupling Matrix Analysis

Generated: 2026-08-07 (verified from source — all imports + SQL parsed)

## Summary

- **Total engine modules analyzed**: 40 (31 top-level engine/*.py + 9 runner/strategies sub-package)
- **Modules with direct DB calls** (get_connection or sqlite3.connect): 19
- **Modules using WriteQueue**: 5 (database, ledger, reconciler, runner.cycle_loop, write_queue itself)
- **Modules with INV-31 violations**: 9

---

## Coupling Matrix (Top-Level engine/*.py)

| Module | Imports From (engine) | Imported By (engine) | get_conn | sqlite3.connect | .execute() | .commit() | INSERT/UPD/DEL total | On trades/bot_orders | WriteQueue | INV-31 Violation |
|--------|-----------------------|-----------------------|----------|-----------------|------------|-----------|---------------------|----------------------|------------|-------------------|
| **bot_executor** | database, exceptions, exchange_interface, ledger, manager, oneway_netting, parity_gates, shutdown_control, strategies.martingale_strategy, ws_cache | ledger, reconciler, runner.__init__, runner.cycle_loop, runner.startup | 14 | 0 | 149 | 45 | 61 | 56 | ❌ | **YES** |
| **bot_management** | — | manager | 1 | 0 | 1 | 0 | 0 | 0 | ❌ | NO |
| **database** | exchange_interface, ledger, parity_gates, runner, strategies.martingale_strategy, write_queue | bot_executor, exchange_interface, ground_truth_reconciler, health, integrity, ledger, manager, metrics, oneway_netting, parity_gates, preflight, reconciler, recovery, risk_manager, run_engine, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup, trading_controls, wipe_proof, ws_event_handlers | 75 | 2 | 368 | 97 | 130 | 61 | ✅ | NO (WQ owner) |
| **exceptions** | — | bot_executor, exchange_interface | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **exchange_interface** | database, exceptions, ledger | bot_executor, database, ground_truth_reconciler, ledger, manager, oneway_netting, parity_gates, preflight, reconciler, risk_manager, run_engine, run_reconciliation, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup, wipe_proof, ws_cache, ws_event_handlers | 0 | 0 | 6 | 0 | 4 | 3 | ❌ | **YES** |
| **ground_truth_reconciler** | database, exchange_interface | runner.startup | 0 | 0 | 19 | 5 | 11 | 4 | ❌ | **YES** |
| **health** | database | — | 0 | 6 | 13 | 0 | 0 | 0 | ❌ | NO |
| **indicators** | — | strategies.martingale_strategy | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **integrity** | database, ledger | runner.__init__, runner.cycle_loop | 2 | 0 | 6 | 0 | 0 | 0 | ❌ | NO |
| **ledger** | bot_executor, database, exchange_interface, ledger, parity_gates, runner, write_queue | bot_executor, database, exchange_interface, integrity, ledger, oneway_netting, parity_gates, reconciler, run_engine, runner.cycle_loop, runner.shutdown, runner.startup, ws_event_handlers | 11 | 0 | 64 | 16 | 27 | 18 | ✅ | NO |
| **manager** | bot_management, database, exchange_interface, risk_manager, strategies.martingale_strategy | bot_executor | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **metrics** | database | run_engine, runner.__init__, runner.cycle_loop | 1 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **oneway_netting** | database, exchange_interface, ledger, parity_gates | bot_executor, reconciler, runner.startup | 3 | 0 | 33 | 7 | 10 | 4 | ❌ | **YES** |
| **order_manager** | — | — | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **parity_gates** | database, exchange_interface, ledger | bot_executor, database, ledger, oneway_netting, reconciler, recovery, runner.cycle_loop, runner.startup, wipe_proof, ws_event_handlers | 11 | 0 | 31 | 7 | 9 | 3 | ❌ | **YES** |
| **preflight** | database, exchange_interface | run_engine | 0 | 3 | 5 | 0 | 0 | 0 | ❌ | NO |
| **reconciler** | bot_executor, database, exchange_interface, ledger, oneway_netting, parity_gates, write_queue | run_reconciliation, runner.__init__, runner.startup, ws_event_handlers | 33 | 0 | 115 | 25 | 39 | 30 | ✅ | NO |
| **reconciler_wipe_audit** | — | — | 0 | 0 | 2 | 0 | 1 | 1 | ❌ | **YES** |
| **recovery** | database, parity_gates | runner.cycle_loop | 1 | 0 | 4 | 3 | 3 | 3 | ❌ | **YES** |
| **risk** | — | — | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **risk_manager** | database, exchange_interface | manager | 2 | 0 | 2 | 0 | 0 | 0 | ❌ | NO |
| **run_engine** | database, exchange_interface, ledger, metrics, preflight, runner, runner.shutdown, shutdown_control, websocket_server, ws_event_handlers | — | 0 | 1 | 1 | 1 | 1 | 0 | ❌ | NO |
| **run_reconciliation** | exchange_interface, reconciler | — | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **shutdown_control** | — | bot_executor, run_engine, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **trading_controls** | database | — | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **websocket_handler** | — | runner.websocket_lifecycle | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **websocket_server** | — | run_engine | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **wipe_proof** | database, exchange_interface, parity_gates | — | 0 | 0 | 2 | 0 | 1 | 1 | ❌ | **YES** |
| **write_queue** | — | database, ledger, reconciler, runner.cycle_loop | 0 | 0 | 0 | 0 | 0 | 0 | ✅ | NO |
| **ws_cache** | exchange_interface | bot_executor, runner.__init__, runner.cycle_loop, ws_event_handlers | 0 | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **ws_event_handlers** | database, exchange_interface, ledger, parity_gates, reconciler, ws_cache | run_engine, runner.shutdown, runner.websocket_lifecycle | 4 | 0 | 8 | 2 | 2 | 2 | ❌ | **YES** |

---

## Coupling Matrix (Sub-Packages: runner/, strategies/)

| Module | Imports From (engine) | Imported By (engine) | get_conn | .execute() | .commit() | INSERT/UPD/DEL total | On trades/bot_orders | WriteQueue | INV-31 Violation |
|--------|-----------------------|-----------------------|----------|------------|-----------|---------------------|----------------------|------------|-------------------|
| **runner.__init__** | bot_executor, database, exchange_interface, integrity, metrics, reconciler, runner, runner.cycle_loop, runner.shutdown, runner.startup, runner.websocket_lifecycle, shutdown_control, strategies.martingale_strategy, ws_cache | — | 2 | 2 | 0 | 0 | 0 | ❌ | NO |
| **runner.cycle_loop** | bot_executor, database, exchange_interface, integrity, ledger, metrics, parity_gates, recovery, shutdown_control, write_queue, ws_cache | runner.__init__ | 7 | 17 | 8 | 11 | 4 | ✅ | NO |
| **runner.shutdown** | database, exchange_interface, ledger, shutdown_control, ws_event_handlers | run_engine, runner.__init__ | 1 | 0 | 1 | 0 | 0 | ❌ | NO |
| **runner.startup** | bot_executor, database, exchange_interface, ground_truth_reconciler, ledger, oneway_netting, parity_gates, reconciler, shutdown_control, strategies.martingale_strategy | runner.__init__ | 1 | 2 | 0 | 0 | 0 | ❌ | NO |
| **runner.websocket_lifecycle** | websocket_handler, ws_event_handlers | runner.__init__ | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **strategies.base** | — | strategies.magic_hour_strategy, strategies.market_maker, strategies.martingale_strategy | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **strategies.magic_hour_strategy** | strategies.base | — | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **strategies.market_maker** | strategies.base | — | 0 | 0 | 0 | 0 | 0 | ❌ | NO |
| **strategies.martingale_strategy** | indicators, strategies.base | bot_executor, database, manager, runner.__init__, runner.startup | 0 | 0 | 0 | 0 | 0 | ❌ | NO |

---

## Import Dependency Graph (Engine → Engine)

```
bot_executor                             → database, exceptions, exchange_interface, ledger, manager, oneway_netting, parity_gates, shutdown_control, strategies.martingale_strategy, ws_cache
database                                 → exchange_interface, ledger, parity_gates, runner, strategies.martingale_strategy, write_queue
exchange_interface                       → database, exceptions, ledger
ground_truth_reconciler                  → database, exchange_interface
health                                   → database
integrity                                → database, ledger
ledger                                   → bot_executor, database, exchange_interface, ledger, parity_gates, runner, write_queue
manager                                  → bot_management, database, exchange_interface, risk_manager, strategies.martingale_strategy
metrics                                  → database
oneway_netting                           → database, exchange_interface, ledger, parity_gates
parity_gates                             → database, exchange_interface, ledger
preflight                                → database, exchange_interface
reconciler                               → bot_executor, database, exchange_interface, ledger, oneway_netting, parity_gates, write_queue
recovery                                 → database, parity_gates
risk_manager                             → database, exchange_interface
run_engine                               → database, exchange_interface, ledger, metrics, preflight, runner, runner.shutdown, shutdown_control, websocket_server, ws_event_handlers
run_reconciliation                       → exchange_interface, reconciler
runner.__init__                          → bot_executor, database, exchange_interface, integrity, metrics, reconciler, runner, runner.cycle_loop, runner.shutdown, runner.startup, runner.websocket_lifecycle, shutdown_control, strategies.martingale_strategy, ws_cache
runner.cycle_loop                        → bot_executor, database, exchange_interface, integrity, ledger, metrics, parity_gates, recovery, shutdown_control, write_queue, ws_cache
runner.shutdown                          → database, exchange_interface, ledger, shutdown_control, ws_event_handlers
runner.startup                           → bot_executor, database, exchange_interface, ground_truth_reconciler, ledger, oneway_netting, parity_gates, reconciler, shutdown_control, strategies.martingale_strategy
runner.websocket_lifecycle               → websocket_handler, ws_event_handlers
strategies.magic_hour_strategy           → strategies.base
strategies.market_maker                  → strategies.base
strategies.martingale_strategy           → indicators, strategies.base
trading_controls                         → database
wipe_proof                               → database, exchange_interface, parity_gates
ws_cache                                 → exchange_interface
ws_event_handlers                        → database, exchange_interface, ledger, parity_gates, reconciler, ws_cache
```

### Reverse Dependencies (Who Imports What)

| Module | Importers |
|--------|-----------|
| bot_executor | ledger, reconciler, runner.__init__, runner.cycle_loop, runner.startup |
| bot_management | manager |
| database | bot_executor, exchange_interface, ground_truth_reconciler, health, integrity, ledger, manager, metrics, oneway_netting, parity_gates, preflight, reconciler, recovery, risk_manager, run_engine, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup, trading_controls, wipe_proof, ws_event_handlers |
| exceptions | bot_executor, exchange_interface |
| exchange_interface | bot_executor, database, ground_truth_reconciler, ledger, manager, oneway_netting, parity_gates, preflight, reconciler, risk_manager, run_engine, run_reconciliation, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup, wipe_proof, ws_cache, ws_event_handlers |
| ground_truth_reconciler | runner.startup |
| indicators | strategies.martingale_strategy |
| integrity | runner.__init__, runner.cycle_loop |
| ledger | bot_executor, database, exchange_interface, integrity, ledger, oneway_netting, parity_gates, reconciler, run_engine, runner.cycle_loop, runner.shutdown, runner.startup, ws_event_handlers |
| manager | bot_executor |
| metrics | run_engine, runner.__init__, runner.cycle_loop |
| oneway_netting | bot_executor, reconciler, runner.startup |
| parity_gates | bot_executor, database, ledger, oneway_netting, reconciler, recovery, runner.cycle_loop, runner.startup, wipe_proof, ws_event_handlers |
| preflight | run_engine |
| reconciler | run_reconciliation, runner.__init__, runner.startup, ws_event_handlers |
| recovery | runner.cycle_loop |
| risk_manager | manager |
| runner | database, ledger, run_engine, runner.__init__ |
| runner.cycle_loop | runner.__init__ |
| runner.shutdown | run_engine, runner.__init__ |
| runner.startup | runner.__init__ |
| runner.websocket_lifecycle | runner.__init__ |
| shutdown_control | bot_executor, run_engine, runner.__init__, runner.cycle_loop, runner.shutdown, runner.startup |
| strategies.base | strategies.magic_hour_strategy, strategies.market_maker, strategies.martingale_strategy |
| strategies.martingale_strategy | bot_executor, database, manager, runner.__init__, runner.startup |
| websocket_handler | runner.websocket_lifecycle |
| websocket_server | run_engine |
| write_queue | database, ledger, reconciler, runner.cycle_loop |
| ws_cache | bot_executor, runner.__init__, runner.cycle_loop, ws_event_handlers |
| ws_event_handlers | run_engine, runner.shutdown, runner.websocket_lifecycle |

---

## INV-31 Violation Details

**INV-31 Rule**: All trades/bot_orders writes must go through WriteQueue singleton.

### Critical Violations (Direct INSERT/UPDATE/DELETE on trades/bot_orders, NO WriteQueue)

| Module | Writes on trades/bot_orders | All INSERT/UPD/DEL | .commit() | Primary Tables |
|--------|----------------------------|---------------------|-----------|----------------|
| **bot_executor** | 56 | 61 | 45 | trades, bot_orders, bots |
| **ground_truth_reconciler** | 4 | 11 | 5 | trades, bot_orders, bots |
| **oneway_netting** | 4 | 10 | 7 | trades, bot_orders, bots |
| **exchange_interface** | 3 | 4 | 0 | trades, bot_orders, bots |
| **parity_gates** | 3 | 9 | 7 | trades, bot_orders, bots |
| **recovery** | 3 | 3 | 3 | trades, bot_orders, bots |
| **ws_event_handlers** | 2 | 2 | 2 | trades, bot_orders, bots |
| **reconciler_wipe_audit** | 1 | 1 | 0 | trades, bot_orders, bots |
| **wipe_proof** | 1 | 1 | 0 | trades, bot_orders, bots |

### Modules With Other Direct DB Writes (NOT on trades/bot_orders — lower INV-31 risk)

| Module | All INSERT/UPD/DEL | .commit() | Notes |
|--------|-------------------|-----------|-------|
| run_engine | 1 | 1 | Writes on bots/active_positions only |

---

## Module Categories by Coupling Risk

### 🔴 **High Risk — Direct trades/bot_orders writes, no WriteQueue**

- **engine.bot_executor**: 56 direct writes on trades/bot_orders, 45 commits
- **engine.ground_truth_reconciler**: 4 direct writes on trades/bot_orders, 5 commits
- **engine.oneway_netting**: 4 direct writes on trades/bot_orders, 7 commits
- **engine.exchange_interface**: 3 direct writes on trades/bot_orders, 0 commits
- **engine.parity_gates**: 3 direct writes on trades/bot_orders, 7 commits
- **engine.recovery**: 3 direct writes on trades/bot_orders, 3 commits
- **engine.ws_event_handlers**: 2 direct writes on trades/bot_orders, 2 commits
- **engine.reconciler_wipe_audit**: 1 direct writes on trades/bot_orders, 0 commits
- **engine.wipe_proof**: 1 direct writes on trades/bot_orders, 0 commits

### 🟢 **Compliant — Uses WriteQueue or has zero DB writes**

- **engine.database** — WriteQueue user
- **engine.exceptions** — zero DB writes
- **engine.indicators** — zero DB writes
- **engine.ledger** — WriteQueue user
- **engine.manager** — zero DB writes
- **engine.metrics** — zero DB writes
- **engine.order_manager** — zero DB writes
- **engine.reconciler** — WriteQueue user
- **engine.risk** — zero DB writes
- **engine.run_reconciliation** — zero DB writes
- **engine.runner.cycle_loop** — WriteQueue user
- **engine.runner.shutdown** — zero DB writes
- **engine.runner.websocket_lifecycle** — zero DB writes
- **engine.shutdown_control** — zero DB writes
- **engine.strategies.base** — zero DB writes
- **engine.strategies.magic_hour_strategy** — zero DB writes
- **engine.strategies.market_maker** — zero DB writes
- **engine.strategies.martingale_strategy** — zero DB writes
- **engine.trading_controls** — zero DB writes
- **engine.websocket_handler** — zero DB writes
- **engine.websocket_server** — zero DB writes
- **engine.write_queue** — WriteQueue user
- **engine.ws_cache** — zero DB writes

---

## Recommended Fix Priority (Phase 1 Task 3)

| Priority | Module | Action |
|----------|--------|--------|
| 1 | **engine.bot_executor** | Refactor 56 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 2 | **engine.ground_truth_reconciler** | Refactor 4 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 3 | **engine.oneway_netting** | Refactor 4 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 4 | **engine.exchange_interface** | Refactor 3 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 5 | **engine.parity_gates** | Refactor 3 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 6 | **engine.recovery** | Refactor 3 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 7 | **engine.ws_event_handlers** | Refactor 2 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 8 | **engine.reconciler_wipe_audit** | Refactor 1 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |
| 9 | **engine.wipe_proof** | Refactor 1 direct writes on trades/bot_orders to WriteQueue.put_and_wait() |

---

## Notes

1. **database.py** owns the WriteQueue singleton (instantiates it, routes writes through it) — its direct writes are compliant.
2. **ledger.py** and **reconciler.py** correctly use `WriteQueue().put_and_wait()` for their public write methods.
3. **runner.cycle_loop** imports WriteQueue (the only runner module that does) — partial compliance.
4. **ws_event_handlers.py** uses its own `_db_write_queue` (a separate `queue.Queue`), NOT the WriteQueue singleton — this is a parallel write path that should be unified into WriteQueue.
5. **bot_executor.py** is the single largest violator: 56 direct INSERT/UPDATE/DELETE on trades/bot_orders tables, 45 commits, zero WriteQueue usage.
6. Write counts reflect actual INSERT/UPDATE/DELETE/REPLACE statements parsed from SQL strings — handles triple-quoted multi-line strings, not just single-line grep.
7. `get_connection` counts include both module-level and inline/dynamic imports (`from engine.database import get_connection as _gc`).
8. Import dependencies include dynamic/inline imports (inside functions), not just module-level — critical for bot_executor.py which has 30+ inline imports.
9. **exchange_interface.py** has 3 writes on trades/bot_orders via inline imports of `update_order_status` and `credit_fill` — but it delegates to database/ledger functions which use WriteQueue internally, so the actual write path may be compliant. Manual review needed.
10. **recovery.py** has 3 writes on trades/bot_orders — these are in crash recovery paths that may intentionally bypass WriteQueue for atomicity. Manual review needed.
11. **reconciler_wipe_audit.py** and **wipe_proof.py** have 1 write each on trades/bot_orders — audit/proof utilities, low volume but still violations.
