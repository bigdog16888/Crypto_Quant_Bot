"""
engine/runner.py — Legacy entry-point shim.
Re-exports BotRunner and SocketLock from the engine.runner package.
Running this file directly starts the engine (same as before).
"""

import sys
import os

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.runner import BotRunner, SocketLock  # noqa: E402, F401

if __name__ == "__main__":
    import time
    import logging
    import signal
    import sqlite3
    import json
    import os as _os_module

    from config.settings import config
    from engine.database import get_connection, init_db
    from engine.metrics import MetricsServer
    from engine.shutdown_control import (
        clear_stop_signal,
        interruptible_sleep,
        is_stop_requested,
        remove_pid,
    )
    from config.constants import MAX_CONSECUTIVE_FAILURES

    logger = logging.getLogger("BotRunner")

    STOP, EMERGENCY = config.PATHS["STOP_FILE"], config.PATHS["EMERGENCY_FILE"]
    shutdown_fast = False

    # --- STEP 1: OS-ENFORCED SINGLETON (SocketLock) ---
    lock = SocketLock()
    if not lock.acquire():
        sys.exit(1)

    init_db()

    # --- STARTUP TIMESTAMP: System-wide grace period gate ---
    try:
        _st_conn = sqlite3.connect(config.PATHS['DB_FILE'], timeout=10)
        _st_conn.execute(
            "INSERT OR REPLACE INTO system_equity (key, value) VALUES ('ENGINE_STARTED_AT', ?)",
            (time.time(),)
        )
        _st_conn.commit()
        _st_conn.close()
        logger.info("✅ [STARTUP] ENGINE_STARTED_AT recorded in system_equity.")
    except Exception as _st_err:
        logger.error(f"Failed to record ENGINE_STARTED_AT (non-fatal): {_st_err}")

    # --- STEP 2: PREFLIGHT CHECK (Startup Gate) ---
    try:
        from engine.preflight import preflight_check
        pf_result = preflight_check()
        if pf_result['passed']:
            logger.info(f"✅ PREFLIGHT PASSED: {pf_result['summary']}")
        else:
            logger.warning(f"⚠️ PREFLIGHT ISSUES: {pf_result['summary']}")
            for issue in pf_result.get('issues', []):
                logger.warning(f"  → {issue}")
    except Exception as e:
        logger.error(f"Preflight check failed (non-fatal): {e}")

    # === METRICS SERVER STARTUP ===
    try:
        metrics_server = MetricsServer(port=config.METRICS_PORT)
        metrics_server.start()
    except Exception as e:
        logger.error(f"FATAL: Failed to start Metrics Server on port {config.METRICS_PORT}: {e}")
        lock.release()
        sys.exit(1)

    logger.info("Bot Service Started.")
    try:
        runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        metrics_server.stop()
        remove_pid()
        clear_stop_signal()
        lock.release()
        sys.exit(1)

    if is_stop_requested():
        logger.info("🛑 Stop signal present after startup — exiting without main loop.")
        runner.running = False
        shutdown_fast = True
    else:
        runner.running = True

    # 🚀 ROOT CAUSE FIX: Graceful Signal Handling
    # Uses ShutdownMixin._graceful_shutdown(self, signum, frame) via bound method.
    # Python's signal.signal passes (signum, frame) to the bound method, which
    # receives self automatically — matching the 3-parameter signature exactly.
    signal.signal(signal.SIGTERM, runner._graceful_shutdown)
    signal.signal(signal.SIGINT, runner._graceful_shutdown)

    if os.path.exists(EMERGENCY):
        os.remove(EMERGENCY)
        logger.info("Cleared stale emergency file")

    failures = 0
    last_heartbeat = 0
    last_cleanup = 0
    cycle_sleep = 15.0

    if not runner.running:
        shutdown_fast = True
        remove_pid()
        clear_stop_signal()
        lock.release()
        logger.info("🛑 Exiting before main loop (stop during startup or init abort).")
    else:
        clear_stop_signal()
        from engine.exchange_interface import cleanup_caches
        from engine.websocket_server import WebSocketServer

        ws_server = None
        try:
            logger.info("Starting WebSocket Server thread...")
            ws_server = WebSocketServer(port=8765)
            ws_server.start()
            logger.info("WebSocket Server thread started.")
        except Exception as e:
            logger.error(f"Failed to start WebSocket Server: {e}")

    while runner.running:
        try:
            if os.path.exists(EMERGENCY):
                logger.critical("🚨 EMERGENCY LIQUIDATION SIGNAL received. Closing all positions...")
                os.remove(EMERGENCY)
                try:
                    runner.handle_emergency_liquidation()
                    logger.critical("✅ Emergency liquidation complete.")
                except Exception as _em_err:
                    logger.critical(f"Emergency liquidation failed: {_em_err}")
                runner.running = False
                break

            if is_stop_requested():
                logger.info("🛑 STOP signal received. Releasing lock and shutting down...")
                shutdown_fast = True
                clear_stop_signal()
                remove_pid()
                lock.release()
                runner.running = False
                break

            now = time.time()

            if now - last_cleanup > 60:
                cleanup_caches()
                last_cleanup = now

            result = runner.run_cycle()

            if ws_server and ws_server.is_alive() and ws_server.loop and not ws_server.loop.is_closed():
                logger.debug(f"[WS_RUNNER] Attempting broadcast. WS server running: {ws_server.running}, loop running: {ws_server.loop.is_running()}")
                try:
                    from engine.database import get_all_bots
                    bots_data = get_all_bots()
                    payload = {
                        "type": "update",
                        "timestamp": time.time(),
                        "bots": bots_data
                    }
                    ws_server.broadcast(payload)
                except Exception as wse:
                     logger.error(f"WS Broadcast Error: {wse}")

            if result is False:
                break

            if isinstance(result, (int, float)) and result > 0:
                cycle_sleep = result
            else:
                cycle_sleep = 15.0

            failures = 0

            if time.time() - last_heartbeat > 60:
                logger.info("💓 System Heartbeat - Active")
                last_heartbeat = time.time()

        except Exception as e:
            failures += 1
            logger.error(f"Cycle failed ({failures}): {e}", exc_info=True)
            cycle_sleep = 15.0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                break
        except BaseException as e:
            logger.critical(f"🛑 FATAL RUNNER ERROR: {e}", exc_info=True)
            break

        if interruptible_sleep(cycle_sleep):
            shutdown_fast = True
            clear_stop_signal()
            remove_pid()
            lock.release()
            runner.running = False
            break

    # === SHUTDOWN SEQUENCE ===
    metrics_server.stop()
    remove_pid()
    clear_stop_signal()

    _db_timeout = 3.0 if shutdown_fast else 15.0
    try:
        from engine.ws_event_handlers import stop_db_worker
        logger.info(f"Flushing async DB write queue before exit (timeout={_db_timeout}s)...")
        stop_db_worker(timeout=_db_timeout)
        logger.info("✅ DB write queue flushed.")
    except Exception as e:
        logger.error(f"Failed to flush DB write queue: {e}")

    if not shutdown_fast:
        try:
            if runner and runner._reconciler:
                logger.info("Executing final pre-shutdown fill audit...")
                runner._reconciler._audit_pending_exits()
                runner._reconciler._audit_pending_grids()
        except Exception as e:
            logger.error(f"[SHUTDOWN-AUDIT] Failed executing pending exits/grids audit on exit: {e}")

        try:
            from engine.ledger import seal_all_active_bots
            corrected = seal_all_active_bots()
            logger.info(f"✅ [SHUTDOWN-SEAL] Sealed {corrected} active bot state(s) from confirmed fills.")
        except Exception as e:
            logger.error(f"[SHUTDOWN-SEAL] Failed to seal bot states on exit: {e}")
    else:
        logger.info("⚡ [FAST-SHUTDOWN] Skipping full seal (stop signal).")

    try:
        with open('last_shutdown.ts', 'w') as _lsf:
            _lsf.write(str(int(time.time())))
        logger.info("✅ [SHUTDOWN] last_shutdown.ts written.")
    except Exception as _lsts_err:
        logger.warning(f"[SHUTDOWN] Could not write last_shutdown.ts: {_lsts_err}")

    lock.release()