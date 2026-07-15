"""
Shutdown mixin for BotRunner.
SocketLock (standalone class) + ShutdownMixin.

STRUCTURAL ADAPTATIONS from original engine/runner.py (all flagged, no logic changes):
  1. _graceful_shutdown was a local closure in __main__ referencing runner.running.
     As a mixin method, runner.running → self.running.  (Mechanical rename.)
  2. The shutdown tail (lines 2051-2091 in original) was free-standing code in __main__
     referencing local variables metrics_server, lock, shutdown_fast.
     It is now a method _run_shutdown_sequence(self, metrics_server, lock, shutdown_fast)
     that takes these as parameters.  The 'runner in locals()' guard is implicit.
  3. handle_emergency_liquidation and _write_pid_file are BotRunner methods moved
     verbatim — zero changes.
"""

import json
import logging
import os
import socket as _socket_module
import time

logger = logging.getLogger("BotRunner")

# ================================================================
# Standalone class — NOT a mixin, NOT inherited by BotRunner.
# Imported and instantiated directly by __main__.
# ================================================================
class SocketLock:
    """
    OS-enforced singleton using TCP port binding.
    - Zero race conditions: OS guarantees only one process can bind a port.
    - Auto-releases on crash: OS reclaims the port when the process dies.
    - No stale files: Nothing on disk to clean up.
    """
    LOCK_PORT = 19888

    def __init__(self, port=None):
        self.port = port or self.LOCK_PORT
        self._socket = None

    def acquire(self):
        self._socket = _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_STREAM)
        self._socket.setsockopt(_socket_module.SOL_SOCKET, _socket_module.SO_REUSEADDR, 0)
        try:
            self._socket.bind(("127.0.0.1", self.port))
            self._socket.listen(1)
            logger.info(f"✅ SocketLock acquired on port {self.port} (PID {os.getpid()})")
            return True
        except OSError as e:
            logger.critical(f"🛑 FATAL: Runner already active (port {self.port} in use). Cannot start duplicate. Error: {e}")
            self._socket.close()
            self._socket = None
            return False

    def release(self):
        if self._socket:
            try:
                self._socket.close()
                logger.info(f"🔓 SocketLock released (port {self.port})")
            except Exception:
                pass
            self._socket = None


# ================================================================
# ShutdownMixin — inherited by BotRunner.
# ================================================================
class ShutdownMixin:
    """Socket-lock, PID file, graceful shutdown, shutdown sequence, emergency liquidation."""

    def _write_pid_file(self):
        """Write PID file for UI status detection (Streamlit reads this)."""
        try:
            from engine.shutdown_control import write_pid
            write_pid()
        except Exception as e:
            logger.error(f"Failed to write PID file: {e}")

    def _graceful_shutdown(self, signum, frame):
        """
        Signal handler for SIGTERM/SIGINT.
        ADAPTATION: original had 'runner.running = False' (closure var);
        as mixin method this becomes 'self.running = False'.
        """
        logger.info(f"Signal {signum} received. Initiating graceful shutdown...")
        # Step 1: Stop WS stream immediately — no new fills can enter the queue.
        try:
            from engine.ws_event_handlers import stop_ws_stream
            stop_ws_stream()
            logger.info("✅ [SHUTDOWN] WS stream stopped.")
        except Exception as _ws_err:
            logger.warning(f"[SHUTDOWN] Could not stop WS stream: {_ws_err}")
        self.running = False  # Triggers the main loop exit

    def handle_emergency_liquidation(self):
        """
        Emergency liquidation for all active bots.
        BUG FIX: Now properly handles futures positions.
        """
        from engine.database import get_connection, reset_bot_after_tp
        from engine.exchange_interface import normalize_market_type
        from config.settings import config

        bots = self.get_active_bots()
        for bot in bots:
            id, name, pair = bot[0], bot[1], bot[2]
            config_json = bot[5]
            config_dict = json.loads(config_json) if config_json else {}
            mt = normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE))
            ex = self.exchanges.get(mt, self.exchange)

            if not ex:
                logger.error(f"Cannot liquidate {name}: No exchange interface available")
                continue

            try:
                ex.cancel_orders_by_bot_id(id, pair)

                if not config.DRY_RUN and mt in ['future', 'swap']:
                    # For futures, fetch positions properly
                    try:
                        positions = ex.exchange.fetch_positions()
                        # Normalize symbol for comparison
                        target_pair_clean = pair.replace('/', '').split(':')[0]

                        for pos in positions:
                            if not pos: continue
                            pos_symbol = pos.get('symbol', '').replace('/', '').split(':')[0]

                            if pos_symbol == target_pair_clean:
                                if float(pos.get('contracts', 0) or pos.get('size', 0) or 0) != 0:
                                    qty = float(pos.get('contracts', 0) or pos.get('size', 0))
                                    side = 'sell' if qty > 0 else 'buy'  # Short if long, Long if short
                                    close_qty = abs(qty)
                                    logger.warning(f"Emergency Market Close {close_qty} {pair} for {name}")
                                    _audit_conn = get_connection()
                                    _audit_cursor = _audit_conn.cursor()
                                    ex.create_order(
                                        pair, 'market', side, close_qty,
                                        emergency=True,
                                        _audit_cursor=_audit_cursor,
                                        _call_site="runner.emergency_liquidate:1476",
                                        human_approved=True
                                    )
                                    _audit_conn.commit()

                                    # CRITICAL FIX: Update DB to reflect closure
                                    # We use reset_bot_after_tp to clear the trade record
                                    # Passing 0 as exit price since it's a panic close (or use current price if available)
                                    try:
                                        reset_bot_after_tp(id, exit_price=0.0, action_label='EMERGENCY_CLOSE', human_approved=True)
                                        logger.warning(f"✅ Bot {name} Database Reset after Emergency Close")
                                    except Exception as db_err:
                                        logger.error(f"Failed to reset DB for {name}: {db_err}")

                    except Exception as pos_err:
                        logger.error(f"Failed to fetch positions for {pair}: {pos_err}")

            except Exception as e: logger.error(f"Cleanup failed for {name}: {e}")

    def _run_shutdown_sequence(self, metrics_server, lock, shutdown_fast=False):
        """
        Post-main-loop shutdown: metrics stop, PID cleanup, DB flush,
        fill audit, seal, last_shutdown.ts, lock release.

        ADAPTATION from original engine/runner.py __main__ block:
        metrics_server, lock, and shutdown_fast are passed as parameters
        since they were local variables in __main__.  The
        'runner in locals()' guard is implicit since self is the runner.
        """
        # === METRICS SERVER STOP ===
        metrics_server.stop()
        try:
            from engine.shutdown_control import remove_pid, clear_stop_signal
        except ImportError:
            pass
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
                if self._reconciler:
                    logger.info("Executing final pre-shutdown fill audit...")
                    self._reconciler._audit_pending_exits()
                    self._reconciler._audit_pending_grids()
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

        # Record clean-shutdown timestamp so startup_sync can compute the real offline window.
        try:
            with open('last_shutdown.ts', 'w') as _lsf:
                _lsf.write(str(int(time.time())))
            logger.info("✅ [SHUTDOWN] last_shutdown.ts written.")
        except Exception as _lsts_err:
            logger.warning(f"[SHUTDOWN] Could not write last_shutdown.ts: {_lsts_err}")

        lock.release()