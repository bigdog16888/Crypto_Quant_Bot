"""
engine.runner — BotRunner engine orchestration (Package).
Module 1 of 4: ShutdownMixin extracted. All other methods remain here
in BotRunner's class body until extracted into their own mixin modules.

Import pattern:
    from engine.runner import BotRunner
    from engine.runner.shutdown import SocketLock
"""

import time
import logging
import json
import sys
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import threading
import psutil
import signal
from logging.handlers import RotatingFileHandler

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, get_starting_equity, update_active_positions_snapshot, update_full_snapshot, update_active_positions
import sqlite3
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.bot_executor import BotExecutor
import engine.bot_executor
from engine.metrics import MetricsServer, BOT_CYCLE_TIME
from engine.integrity import enforce_integrity
from engine.ws_cache import get_ws_cache

from engine.runner.shutdown import ShutdownMixin, SocketLock
from engine.runner.websocket_lifecycle import WebSocketLifecycleMixin
from engine.runner.startup import StartupMixin
from engine.runner.cycle_loop import CycleLoopMixin

from config.settings import config
from config.constants import (
    MIN_ORDER_USD,
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
    POLL_INTERVAL_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    STABLECOINS
)

# Configure logging with rotation (Max 10MB, keep 5 backups)
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = config.PATHS["LOG_FILE"]

rotating_handler = RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
rotating_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    handlers=[rotating_handler, stream_handler]
)
logger = logging.getLogger("BotRunner")

# NOISE REDUCTION: Silence non-critical network warnings
logging.getLogger('ccxt').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('web3').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

class BotRunner(StartupMixin, ShutdownMixin, WebSocketLifecycleMixin, CycleLoopMixin):
    _instance = None

    def _abort_if_stop_requested(self, phase: str) -> bool:
        """Return True if cooperative stop was requested (sets running=False)."""
        from engine.shutdown_control import is_stop_requested
        if is_stop_requested():
            logger.info(f"🛑 Stop requested during {phase} — aborting.")
            self.running = False
            return True
        return False

    @classmethod
    def get_instance(cls):
        """Returns the current runner instance (for singleton access)"""
        return cls._instance

    def get_strategy(self, bot_id, params):
        """
        Retrieves or creates a strategy instance for a given bot.
        Caches instances to avoid recreation overhead.
        """
        if bot_id in self.strategies:
            return self.strategies[bot_id]

        # Create new strategy instance
        try:
            strategy = MartingaleStrategy(params)
            self.strategies[bot_id] = strategy
            return strategy
        except Exception as e:
            logger.error(f"Failed to create strategy for bot {bot_id}: {e}")
            raise e

    def _calculate_stablecoin_balance(self, balance: dict) -> float:
        """Calculate total balance across USDT and USDC stablecoins."""
        total = 0.0
        for currency in STABLECOINS:
            curr_bal = balance.get(currency)
            if isinstance(curr_bal, dict):
                total += float(curr_bal.get('total', 0.0))
        return total

    def sync_all_bots(self):
        """
        Synchronizes the state of all active bots with the exchange.
        Uses the new comprehensive reconciliation system (v2.0).
        """
        logger.info("Starting comprehensive state reconciliation...")

        if not self.exchanges:
             logger.warning("⚠️ Cannot sync bots: Exchanges not initialized.")
             return

        try:
            # Instantiate Reconciler with current exchanges
            # We must pass the actual exchange instances
            from engine.reconciler import StateReconciler
            reconciler = StateReconciler(self.exchanges)

            # Execute Full Reconciliation
            results = reconciler.reconcile_all()

            # Log summary (v2.0: Actions Taken)
            actions_count = sum(1 for r in results if r.action_taken.value != "no_action")
            zombie_fixes = sum(1 for r in results if r.action_taken.value == "reset_to_idle")
            manual_warnings = sum(1 for r in results if r.requires_manual_intervention)

            logger.info(f"Reconciliation complete: {actions_count} actions taken ({zombie_fixes} zombie resets), {manual_warnings} manual warnings needed.")

            # 🚨 Emit loud alerts for anything requiring manual intervention
            for r in results:
                if r.requires_manual_intervention:
                    logger.warning(
                        f"\n"
                        f"════════════════════════════════════════════════════\n"
                        f" ⚠️  MANUAL INTERVENTION REQUIRED: {r.pair}  ⚠️\n"
                        f"════════════════════════════════════════════════════\n"
                        f" Reason: {r.details}\n"
                        f" Action: Go to Binance Web UI → Positions → {r.pair}\n"
                        f"         Identify which bot (by CQB_ order DNA) owns the gap.\n"
                        f"         Then manually reset the correct bot to match exchange reality.\n"
                        f"════════════════════════════════════════════════════"
                    )

        except Exception as e:
            logger.error(f"❌ Critical Error during State Reconciliation: {e}")

    def _calculate_unrealized_pnl(self, exchange_snapshot=None) -> float:
        """Calculates total unrealized PnL across all active market types."""
        total_unrealized_pnl = 0.0

        # If snapshot provided, use it
        if exchange_snapshot:
            for mt, data in exchange_snapshot.items():
                positions = data.get('positions', [])
                for p in positions:
                    total_unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            return total_unrealized_pnl

        # Fallback (Manual fetch)
        active_market_types = set(self.exchanges.keys())
        for mt in active_market_types:
            try:
                ex = self.exchanges[mt]
                all_positions = ex.fetch_positions()
                for p in all_positions:
                    total_unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            except Exception as e:
                logger.warning(f"Failed to fetch positions for PnL calculation in {mt}: {e}")

        return total_unrealized_pnl

    def check_circuit_breaker(self, exchange_snapshot=None):
        """
        Global Circuit Breaker: Checks if account equity has dropped below safe limits.
        """
        if getattr(config, 'NO_API_MODE', False):
            return

        if self.circuit_breaker_triggered or self.initial_equity <= 0:
            return

        try:
            total_stablecoin = 0.0
            balance_fetch_success = False

            # Prepare active bots for cost calculation
            active_bots_raw = self.get_active_bots()
            active_bots = [b for b in active_bots_raw if b[9] == 1]

            # Use snapshot if available
            if exchange_snapshot:
                for mt, data in exchange_snapshot.items():
                    balance = data.get('balance')
                    if balance:
                        total_stablecoin += self._calculate_stablecoin_balance(balance)
                        balance_fetch_success = True
            else:
                # Fallback to manual fetch
                active_market_types = set()
                for bot in active_bots:
                    config_dict = json.loads(bot[5]) if bot[5] else {}
                    active_market_types.add(normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE)))

                if not active_market_types: active_market_types.add(config.MARKET_TYPE)

                for mt in active_market_types:
                    if mt in self.exchanges:
                        try:
                            balance = self.exchanges[mt].fetch_balance()
                            if balance:
                                total_stablecoin += self._calculate_stablecoin_balance(balance)
                                balance_fetch_success = True
                        except Exception: pass

            if not balance_fetch_success:
                logger.warning("Circuit breaker check skipped - balance fetch failed")
                return

            invested_cost = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and t_data.get('total_invested') and t_data['total_invested'] > 0:
                    invested_cost += float(t_data['total_invested'])

            # Unrealized PnL from snapshot/cache
            unrealized_pnl = 0.0
            if exchange_snapshot:
                for mt, data in exchange_snapshot.items():
                    positions = data.get('positions', [])
                    for p in positions:
                        unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            else:
                # Fallback to manual fetch
                active_market_types = set()
                for bot in active_bots:
                    config_dict = json.loads(bot[5]) if bot[5] else {}
                    active_market_types.add(normalize_market_type(config_dict.get('market_type', config.MARKET_TYPE)))

                if not active_market_types: active_market_types.add(config.MARKET_TYPE)

                for mt in active_market_types:
                    if mt in self.exchanges:
                        try:
                            positions = self.exchanges[mt].fetch_positions()
                            for p in positions:
                                unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
                        except: pass

            current_equity = total_stablecoin + invested_cost + unrealized_pnl

            # Log for debugging
            logger.debug(f"Circuit Check: Equity ${current_equity:.2f} (Cash: {total_stablecoin:.2f} + Cost: {invested_cost:.2f} + uPnL: {unrealized_pnl:.2f})")

            if self.initial_equity > 0:
                drawdown = (self.initial_equity - current_equity) / self.initial_equity * 100
                if drawdown >= config.GLOBAL_STOP_LOSS_PCT:
                    logger.critical(f"CIRCUIT BREAKER TRIGGERED! Drawdown: {drawdown:.2f}%")
                    self.circuit_breaker_triggered = True
                    with open(config.PATHS["EMERGENCY_FILE"], "w") as f:
                        f.write(f"Circuit Breaker Triggered at {drawdown:.2f}% drawdown")
                    self.handle_emergency_liquidation()
        except Exception as e:
            logger.error(f"Circuit breaker check failed: {e}")

    def get_active_bots(self):
        """Fetches all bots and their current status."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Query returns all bots LEFT JOIN trades to get real investment state
            cursor.execute('''
                SELECT b.id, b.name, b.pair, b.direction, b.strategy_type, b.config,
                       COALESCE(t.total_invested, 0),
                       COALESCE(t.current_step, 0),
                       b.rsi_limit, b.is_active,
                       b.base_size, b.martingale_multiplier,
                       b.status
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
            ''')
            bots = cursor.fetchall()
            return bots
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally:
            pass # conn.close() disabled for singleton safety



    def get_expected_active_positions_count(self):
        """Returns count of bots that DB says should have positions."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
            count = cursor.fetchone()[0]
            pass # conn.close() disabled for singleton safety
            return count
        except Exception as e:
            logger.error(f"Failed to get expected positions count: {e}")
            return 0

