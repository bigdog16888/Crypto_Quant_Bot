"""Ghost-sweep regression: the 2026-09-09 13:39 NameError class.

V2.4.2 added three get_step_from_cid() call sites in the duplicate-order sweep;
V2.4.3 renamed the helper to get_cycle_step_from_cid() but missed them. The
sweep crashed with NameError whenever a bot had 2+ resting TPs or grids,
leaving duplicate TPs on the exchange (double-fill risk: TP for the whole
position + a second TP = oversell). Live evidence 2026-09-09: 10016 and 10019
each carried 2 resting TPs for hours, 67 NameErrors each.

These tests drive the REAL maintain-orders path with 2 resting TPs and assert:
the sweep completes (no NameError), keeps exactly 1 TP, and cancels the ghost.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import engine.database as database
from engine.database import init_db, get_connection, save_bot_order

BOT = 92016
PAIR = "BTC/USDC:USDC"


class SweepExchange:
    """Fake exchange with two resting TPs; cancel_order actually removes."""
    def __init__(self):
        self.orders = [
            {"id": "9001", "clientOrderId": f"CQB_{BOT}_TP_20_2_R111", "side": "sell",
             "price": 79313.8, "amount": 0.004, "status": "new",
             "filled": 0.0, "timestamp": int(time.time() * 1000)},
            {"id": "9002", "clientOrderId": f"CQB_{BOT}_TP_20_3", "side": "sell",
             "price": 79849.8, "amount": 0.008, "status": "new",
             "filled": 0.0, "timestamp": int(time.time() * 1000) + 1},
        ]
        self.cancelled = []

    def fetch_open_orders(self, pair=None):
        return [dict(o) for o in self.orders]

    def cancel_order(self, order_id, pair):
        self.cancelled.append(order_id)
        self.orders = [o for o in self.orders if o["id"] != order_id]
        return {"id": order_id, "status": "canceled", "filled": 0.0, "amount": 0.004}

    # seams for the post-sweep maintenance path
    def fetch_positions(self):
        return [{"symbol": PAIR, "contracts": 0.008, "qty": 0.008,
                 "net_qty": 0.008, "side": "long",
                 "unrealizedPnl": 0.0, "entryPrice": 78800.0}]

    def get_symbol_precision(self, pair):
        return {"tick_size": 0.1, "price_precision": 1, "amount_precision": 3, "min_qty": 0.001}

    def get_best_bid_ask(self, pair):
        return 78998.0, 79002.0

    def fetch_order(self, order_id, pair):
        for o in self.orders:
            if o["id"] == order_id:
                return dict(o)
        return None

    def validate_order(self, pair, side, amount, price, is_closing=False):
        return True, amount, price, None

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        return {"id": "9003", "status": "new", "amount": amount, "price": price,
                "clientOrderId": (params or {}).get("newClientOrderId", "")}


class TestGhostSweepNameErrorFixed(unittest.TestCase):
    def setUp(self):
        self.orig_backup = database.backup_database
        database.backup_database = lambda: None
        self.orig_db_path = database.DB_PATH
        self.db_fd, self.db_temp = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        database.DB_PATH = self.db_temp
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        init_db()

        conn = get_connection()
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, rsi_limit,"
            " martingale_multiplier, base_size, is_active, status)"
            " VALUES (?,?,?,?,?,?,?,?,1,'IN TRADE')",
            (BOT, "sweep-test", PAIR, "BTCUSDC", "LONG", 30, 1.88, 160.0),
        )
        conn.execute(
            "INSERT INTO trades (bot_id, current_step, cycle_id, cycle_phase, open_qty,"
            " total_invested, avg_entry_price, entry_confirmed)"
            " VALUES (?, 3, 20, 'ACTIVE', 0.008, 632.0, 78800.0, 1)",
            (BOT,),
        )
        # Two TP rows matching the exchange's two resting TPs (the live shape)
        save_bot_order(BOT, "tp", "9001", 79313.8, 0.004, step=2, status="open",
                       client_order_id=f"CQB_{BOT}_TP_20_2_R111", cycle_id=20)
        save_bot_order(BOT, "tp", "9002", 79849.8, 0.008, step=3, status="open",
                       client_order_id=f"CQB_{BOT}_TP_20_3", cycle_id=20)
        conn.commit()
        self.exchange = SweepExchange()

    def tearDown(self):
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        database.DB_PATH = self.orig_db_path
        database.backup_database = self.orig_backup
        try:
            os.remove(self.db_temp)
        except Exception:
            pass

    def test_no_bare_get_step_from_cid_in_source(self):
        """Code-level: the renamed helper is used at every call site — no bare
        get_step_from_cid reference remains (the NameError cannot recur)."""
        import engine.bot_executor as be
        import inspect
        src = inspect.getsource(be)
        self.assertNotIn("get_step_from_cid(", src,
            "bare get_step_from_cid( call remains — NameError class not fixed")
        self.assertIn("def get_cycle_step_from_cid(", src)

    def test_duplicate_tp_sweep_completes_and_keeps_one(self):
        """Behavioral: with 2 resting TPs (current_step=3 → expected step 4 via
        step+1 semantics: TP covers position after next grid), the sweep must
        NOT raise, must cancel exactly one ghost, and leave exactly 1 resting.
        The pre-fix code raised NameError at the sort key before any cancel."""
        from engine.bot_executor import BotExecutor
        from config.settings import config as app_config

        # Reach the sweep: the function is in maintain_orders path; invoke the
        # executor's real path with our exchange. We assert the NameError is
        # gone by scanning the log capture if the call is internal, and by
        # driving the sort-key block directly:
        # The cleanest real-path driver: process the tp_orders list the way
        # the sweep does, using the REAL nested helper from the module.
        be_src = __import__("engine.bot_executor", fromlist=["BotExecutor"])

        # Drive the real selection logic: fetch both TPs, run the sweep block
        # via the executor's maintain path on a minimal BotRunner harness.
        from engine.runner import BotRunner
        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            runner = BotRunner()
        runner.exchanges = {'future': self.exchange}
        runner.exchange = self.exchange
        executor = BotExecutor(runner)

        bot_status = {
            'cycle_id': 20, 'current_step': 3, 'open_qty': 0.008,
            'entry_confirmed': 1, 'total_invested': 632.0, 'status': 'IN TRADE',
            'cycle_phase': 'ACTIVE', 'avg_entry_price': 78800.0,
            'target_tp_price': 79849.8, 'direction': 'LONG',
            'last_exit_time': 0, 'basket_start_time': int(time.time()) - 3600,
            'tp_order_id': '9002',
        }

        # maintain_orders is the real entry; use the public path. If the sweep
        # hits the old NameError, this raises (test FAILS on pre-fix code).
        try:
            executor.maintain_orders(
                bot_id=BOT, name="sweep-test", pair=PAIR, direction="LONG",
                current_price=79000.0, exchange=self.exchange,
                market_snapshot=None, bot_config={"market_type": "future"},
                bot_status=bot_status,
            )
        except NameError as e:
            self.fail(f"NameError survived the sweep: {e}")

        # Exactly one TP resting after the sweep, and it must be the CORRECT one:
        # the step-matching, qty-matching TP_20_3 (9002) survives; the stale
        # ghost TP_20_2_R (9001) is gone. (The stale-purge and the ghost-sweep
        # both cancel 9001 — count of cancel CALLS may be 2 for the same id.)
        remaining = self.exchange.fetch_open_orders(PAIR)
        tp_remaining = [o for o in remaining if 'TP' in str(o.get('clientOrderId', ''))]
        self.assertEqual(len(tp_remaining), 1,
            f"ghost sweep must leave exactly 1 TP, exchange has {len(tp_remaining)}: "
            f"{[o['clientOrderId'] for o in tp_remaining]}")
        self.assertEqual(tp_remaining[0]['id'], '9002',
            f"the survivor must be the step/qty-matching TP, got {tp_remaining[0]['clientOrderId']}")
        self.assertNotIn('9001', [o['id'] for o in tp_remaining],
            "the stale ghost TP must be gone")


if __name__ == "__main__":
    unittest.main()
