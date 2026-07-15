# Runner Migration — Direct Unit-Test Backlog

This file is the single running list of moved `engine/runner` methods that
lack **direct** unit tests after extraction into `engine/runner/<module>.py`.
Indirect coverage (via tests that mock the method, or exercise it through
`BotRunner`) is noted but does NOT count as direct coverage.

After all four modules are extracted, this list becomes the test-writing
backlog. Each entry: module, method(s), current direct-test status, notes.

---

## Module 2 — `engine/runner/websocket_lifecycle.py`
- `WebSocketLifecycleMixin._ws_health_check` — **COVERED** (direct test added:
  `tests/test_ws_health_check.py`). This is the only method in this module.

## Module 3 — `engine/runner/startup.py`
- `StartupMixin.__init__` — **UNCOVERED** (direct). Indirect: `test_bot_lifecycle`,
  `test_startup_barrier_race`, `test_ghost_clearing` mock/exercise it.
- `StartupMixin._post_init` — **UNCOVERED** (direct). Indirect: same as above.
- `StartupMixin.startup_sync` — **UNCOVERED** (direct). Indirect: same as above.
- `StartupMixin._initialize_exchanges` — **UNCOVERED** (direct). Indirect: same.
- `StartupMixin._initialize_safety_baseline` — **UNCOVERED** (direct). Indirect: same.

## Module 4 — `engine/runner/cycle_loop.py`
- `CycleLoopMixin.run_cycle` — **UNCOVERED** (direct). Indirect: exercised via
  `test_bot_lifecycle`, `test_hedge_lifecycle`, `test_pending_flatten_handler`,
  and the full `pytest` run (orchestration path).
- `CycleLoopMixin._handle_pending_flatten` — **UNCOVERED** (direct). Indirect:
  `test_pending_flatten_handler` exercises the startup-sync repair path; the
  in-cycle handler itself is only indirectly covered.
- `CycleLoopMixin._handle_pending_close` — **UNCOVERED** (direct). Indirect:
  exercised via `test_bot_lifecycle` close paths.
- `CycleLoopMixin._process_directive` (if present) — **UNCOVERED** (direct).

## Module 1 — `engine/runner/shutdown.py`
- `ShutdownMixin.stop_engine` — **UNCOVERED** (direct). Indirect: exercised via
  `test_bot_lifecycle` shutdown paths.
- `ShutdownMixin._write_pid_file` — **UNCOVERED** (direct).
- `ShutdownMixin._release_socket_lock` — **UNCOVERED** (direct).
- `ShutdownMixin._fast_shutdown` — **UNCOVERED** (direct).
- `SocketLock` class — **UNCOVERED** (direct).