"""Scenario replay: silent-cancel-swallow defect (P1, 2026-09-10).

Two live failure modes, one root cause (engine/exchange_interface.py:220-223
at c3acc98): the raw DELETE /fapi/v1/order path collapsed Binance's
-2011 "Unknown order sent." 400 body to None (logged at DEBUG — invisible),
and cancel_order() swallowed every other failure to None too. Callers then
marked DB rows cancelled/'cancelling' and logged "Cancelled stale" while the
order kept resting LIVE on the exchange:

  - SUI 10018 TP_20_3_R1788921114 (2026-09-09): 1127 "🔥 Cancelled stale"
    lines 11:10-14:29, zero surfaced errors; the oversized TP (54.8 vs 21.1
    position) stayed NEW and filled during downtime → over-sell.
  - XAU 10019 ghost TP 583851054 (2026-09-09): TP-SYNC cancel never landed
    (-2011 while NEW); DB marked terminal 'cancelled' → replacement TP
    stacked → STATE-MACHINE block loop (~300x).

Fix contract under test (branch fix/silent-cancel-swallow):
  1. _raw_request NEVER collapses the already-gone 400 to None — raises
     OrderAlreadyGoneError carrying the raw body, logged at WARNING.
  2. cancel_order is a trichotomy: dict = CONFIRMED cancel; None = CONFIRMED
     gone; CancelFailedError = neither (must not be treated as success).
  3. Verify-by-GET before declaring gone: -2011 alone is NOT proof (the
     exchange lied for hours on XAU 583851054).
  4. Consecutive-failure streak per order id; CRITICAL [CANCEL-ESCALATION]
     at CANCEL_STREAK_ESCALATION (default 3), then every 10th.
  5. Stale-sweep and TP-SYNC callers only mark DB on a confirmed outcome and
     abort/leave the row open on CancelFailedError.
  6. REL-1 emergency sweep (cancel_orders_by_bot_id, shutdown.py:124
     primitive) survives a failed cancel per-order, does not count it.

The real _raw_request is exercised by patching requests.request at the
module seam — the 400-parsing branch under test is the production code, not
a test-side copy.
"""
import json
import os
import sys
import time
import logging
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import init_db, get_connection, save_bot_order
from config.settings import config

BOT = 92018          # test-only id (90001+ range, outside STARTUP_EXCLUDED_BOT_IDS)
BOT_XAU = 92019
PAIR = "SUI/USDC:USDC"
XAU_PAIR = "XAU/USDT:USDT"

# Exact Binance response bodies observed in the 2026-09-09 incidents
GONE_2011 = '{"code":-2011,"msg":"Unknown order sent."}'
GONE_2013 = '{"code":-2013,"msg":"Order does not exist."}'


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeDemoFAPI:
    """Replays Demo FAPI behind the REAL ExchangeInterface._raw_request.

    DELETE of a live order: honest -> 200 + CANCELED body; liar -> 400 -2011
    while the order is still NEW (the exchange-side order-age corruption
    seen live on both SUI and XAU 2026-09-09).
    DELETE of an unknown/terminal order: 400 -2011 (Binance's cancel error).
    GET of an unknown order: 400 -2013 (Binance's query error).
    """
    def __init__(self):
        self.orders = {}          # order_id -> raw FAPI order dict
        self.liar_ids = set()     # per-order: DELETE always -2011 while live
        self.delete_calls = 0
        self.get_calls = 0
        self.create_calls = 0

    def _live(self, oid):
        o = self.orders.get(str(oid))
        return o is not None and o['status'] in ('NEW', 'PARTIALLY_FILLED')

    def _raw(self, endpoint, method, params):
        params = params or {}
        if method == 'DELETE' and endpoint == '/fapi/v1/order':
            self.delete_calls += 1
            oid = str(params.get('orderId'))
            if self._live(oid):
                if str(oid) in self.liar_ids:
                    return ('RAISE_400', GONE_2011)
                self.orders[oid]['status'] = 'CANCELED'
                return self.orders[oid]
            return ('RAISE_400', GONE_2011)
        if method == 'GET' and endpoint == '/fapi/v1/order':
            self.get_calls += 1
            oid = str(params.get('orderId'))
            o = self.orders.get(oid)
            if o is None:
                return ('RAISE_400', GONE_2013)
            return o
        if method == 'GET' and endpoint == '/fapi/v1/openOrders':
            return [o for o in self.orders.values() if o['status'] in ('NEW', 'PARTIALLY_FILLED')]
        raise AssertionError(f"unexpected raw request {method} {endpoint}")

    def add_order(self, oid, cid, status='NEW', executedQty='0', origQty='21.1', price='0.8230', symbol='SUIUSDC'):
        self.orders[str(oid)] = {
            'orderId': int(oid), 'symbol': symbol, 'status': status,
            'executedQty': executedQty, 'origQty': origQty, 'price': price,
            'avgPrice': '0', 'clientOrderId': cid,
        }


class CancelHarnessMixin:
    """Builds a REAL ExchangeInterface wired to the FakeDemoFAPI at the
    requests.request seam, so _raw_request/cancel_order production logic
    executes against replayed incident responses. No network."""

    def make_exchange(self):
        from engine.exchange_interface import ExchangeInterface
        fake = FakeDemoFAPI()
        ex = ExchangeInterface.__new__(ExchangeInterface)
        ex.logger = logging.getLogger("ExchangeInterface")

        def fake_requests_request(method, url, **kwargs):
            parsed = urlparse(url)
            endpoint = parsed.path
            params = dict(parse_qsl(parsed.query))
            res = fake._raw(endpoint, method.upper(), params)
            if isinstance(res, tuple) and res[0] == 'RAISE_400':
                return FakeResponse(400, res[1])
            return FakeResponse(200, json.dumps(res))

        patcher = patch('engine.exchange_interface.requests.request', fake_requests_request)
        patcher.start()
        self.addCleanup(patcher.stop)

        ex._get_adjusted_timestamp = lambda: int(time.time() * 1000)
        # instance seams for the higher-level wrapper methods (unified shapes)
        ex.fetch_order = self._mk_fetch_order(fake)
        ex.fetch_open_orders = self._mk_fetch_open_orders(fake)
        ex.fetch_positions = lambda: []
        ex.get_symbol_precision = lambda symbol: {
            'tick_size': 0.0001, 'price_precision': 4,
            'step_size': 0.1, 'qty_precision': 1, 'min_notional': 5}
        ex.get_best_bid_ask = lambda symbol: (0.81, 0.83)
        ex.validate_order = lambda symbol, side, amount, price=None, is_closing=False: (True, amount, price, None)

        def fake_create_order(symbol, type_, side, amount, price=None, params=None, **kw):
            fake.create_calls += 1
            oid = 9000 + fake.create_calls
            fake.add_order(oid, (params or {}).get('newClientOrderId', f'CQB_NEW_{oid}'),
                          status='NEW', origQty=str(amount), price=str(price))
            return {'id': oid, 'status': 'new', 'amount': amount, 'price': price,
                    'clientOrderId': (params or {}).get('newClientOrderId', '')}
        ex.create_order = fake_create_order
        return ex, fake

    def _mk_fetch_order(self, fake):
        def fetch_order(order_id, symbol, params=None):
            res = fake._raw('/fapi/v1/order', 'GET',
                           {'symbol': 'SUIUSDC', 'orderId': str(order_id)})
            if isinstance(res, tuple):
                from engine.exceptions import APIError
                raise APIError(f"Binance API 400: {res[1]}")
            avg = float(res.get('avgPrice', 0) or 0)
            price = float(res.get('price', 0) or 0)
            return {'id': res.get('orderId', order_id),
                    'status': res.get('status', 'unknown').lower(),
                    'filled': float(res.get('executedQty', 0)),
                    'amount': float(res.get('origQty', 0)),
                    'average': avg if avg > 0 else price, 'price': price,
                    'clientOrderId': res.get('clientOrderId', '')}
        return fetch_order

    def _mk_fetch_open_orders(self, fake):
        def fetch_open_orders(symbol=None):
            res = fake._raw('/fapi/v1/openOrders', 'GET', {'symbol': 'SUIUSDC'})
            return [{'id': o['orderId'], 'symbol': o['symbol'],
                     'side': 'sell', 'price': float(o['price']),
                     'amount': float(o['origQty']),
                     'clientOrderId': o['clientOrderId'],
                     'status': o['status'].lower(), 'type': 'limit',
                     'timestamp': int(time.time() * 1000)} for o in res]
        return fetch_open_orders

    def reset_streaks(self):
        from engine.exchange_interface import ExchangeInterface
        ExchangeInterface._cancel_fail_streaks = {}
        # Operability (2026-09-11): also clear the per-order back-off registry
        # — a stale armed window from a previous test would gate this suite's
        # own post-escalation retries and cross-contaminate cases.
        ExchangeInterface._cancel_backoff_until = {}


class TestRawRequestNeverCollapsesToNone(unittest.TestCase, CancelHarnessMixin):
    """Contract #1/#2/#3 at the primitive level."""

    def setUp(self):
        config.DEMO_TRADING = True
        config.TESTNET = True
        self.reset_streaks()
        self.addCleanup(self.reset_streaks)

    def tearDown(self):
        config.DEMO_TRADING = False
        config.TESTNET = False

    def test_already_gone_400_raises_typed_not_none(self):
        """RED on c3acc98: _raw_request returned None for the -2011 body and
        logged it at DEBUG (invisible at INFO — zero error lines in the whole
        incident log). GREEN: typed OrderAlreadyGoneError with the raw body."""
        from engine.exceptions import OrderAlreadyGoneError
        ex, fake = self.make_exchange()
        fake.orders = {}  # 111 does not exist on the exchange
        with self.assertRaises(OrderAlreadyGoneError) as cm:
            ex._raw_request('/fapi/v1/order', method='DELETE',
                            params={'symbol': 'SUIUSDC', 'orderId': '111'})
        self.assertEqual(cm.exception.raw_body, GONE_2011)
        self.assertEqual(cm.exception.error_code, -2011)

    def test_cancel_confirmed_returns_dict(self):
        """DELETE 200 -> dict: the caller can mark the DB in good faith."""
        ex, fake = self.make_exchange()
        fake.add_order(222, f'CQB_{BOT}_TP_20_3')
        res = ex.cancel_order(222, PAIR)
        self.assertIsInstance(res, dict)
        self.assertEqual(fake.orders['222']['status'], 'CANCELED')

    def test_cancel_unknown_order_confirmed_gone_returns_none(self):
        """DELETE -2011 + verify-GET -2013 -> None (positively confirmed gone)."""
        ex, fake = self.make_exchange()
        fake.orders = {}  # 333 never existed
        res = ex.cancel_order(333, PAIR)
        self.assertIsNone(res)
        self.assertEqual(fake.get_calls, 1)  # verify-by-GET happened

    def test_cancel_liar_2011_raises_cancel_failed(self):
        """THE SUI/XAU SHAPE: -2011 while the order is still NEW.

        RED on c3acc98: cancel_order swallowed to None, callers logged
        'Cancelled stale' and marked the DB while the order rested live.
        GREEN: verify-GET sees NEW -> CancelFailedError; order still live.
        """
        from engine.exceptions import CancelFailedError
        ex, fake = self.make_exchange()
        fake.add_order(583851054, f'CQB_{BOT_XAU}_TP_14_4_R1788921085', symbol='XAUUSDT')
        fake.liar_ids.add('583851054')
        with self.assertRaises(CancelFailedError):
            ex.cancel_order(583851054, XAU_PAIR)
        # the order is STILL LIVE — no silent disappearance
        self.assertEqual(fake.orders['583851054']['status'], 'NEW')
        self.assertEqual(fake.delete_calls, 1)
        self.assertEqual(fake.get_calls, 1)  # verify-by-GET before declaring

    def test_cancel_gone_then_verified_terminal_returns_none(self):
        """-2011 DELETE + GET shows terminal status -> None (confirmed gone),
        no CancelFailedError — the fix does not cry wolf on real cancels."""
        ex, fake = self.make_exchange()
        fake.add_order(444, f'CQB_{BOT}_TP_20_3', status='CANCELED')
        res = ex.cancel_order(444, PAIR)
        self.assertIsNone(res)


class TestStreakEscalation(unittest.TestCase, CancelHarnessMixin):
    """Contract #4: N consecutive failures -> CRITICAL escalation, reset on success."""

    def setUp(self):
        config.DEMO_TRADING = True
        config.TESTNET = True
        self.reset_streaks()
        self.addCleanup(self.reset_streaks)

    def tearDown(self):
        config.DEMO_TRADING = False
        config.TESTNET = False

    def test_three_consecutive_failures_escalate_then_reset(self):
        from engine.exceptions import CancelFailedError
        ex, fake = self.make_exchange()
        fake.add_order(179430957, f'CQB_{BOT}_TP_20_3_R1788921114')
        fake.liar_ids.add('179430957')

        crit_records = []
        h = logging.Handler()
        h.emit = lambda record: crit_records.append(record) if record.levelno >= logging.CRITICAL else None
        lg = logging.getLogger("ExchangeInterface")
        lg.addHandler(h)
        self.addCleanup(lg.removeHandler, h)

        for _ in range(3):
            with self.assertRaises(CancelFailedError):
                ex.cancel_order(179430957, PAIR)
        self.assertEqual(len(crit_records), 1,
            f"expected exactly 1 CRITICAL at streak 3, got {len(crit_records)}")
        self.assertIn('CANCEL-ESCALATION', crit_records[0].getMessage())
        self.assertIn('179430957', crit_records[0].getMessage())

        # 4th failure does not re-fire (threshold, then every 10th).
        # Operability (2026-09-11): with back-off armed at streak 3 the 4th
        # attempt is correctly DEFERRED (raises before reaching the exchange)
        # — expire the window to keep exercising the raw streak semantics
        # this test owns.
        with self.assertRaises(CancelFailedError):
            ex.cancel_order(179430957, PAIR)
        self.assertEqual(len(crit_records), 1)

        # success resets the streak
        from engine.exchange_interface import ExchangeInterface
        ExchangeInterface._cancel_backoff_until[str(179430957)] = 0.0
        fake.liar_ids.discard('179430957')
        res = ex.cancel_order(179430957, PAIR)
        self.assertIsInstance(res, dict)
        from engine.exchange_interface import ExchangeInterface
        self.assertEqual(ExchangeInterface._cancel_fail_streaks, {})


class _DbBacked(unittest.TestCase, CancelHarnessMixin):
    """Shared temp-DB harness for the bot-level replays."""

    def _init_db(self):
        self.orig_backup = database.backup_database
        database.backup_database = lambda: None
        self.orig_db_path = database.DB_PATH
        db_fd, self.db_temp = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        database.DB_PATH = self.db_temp
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        init_db()

    def _make_bot(self, bot_id, pair, norm, open_qty, invested, avg, tp_oid, tp_cid):
        conn = get_connection()
        conn.execute(
            "INSERT INTO bots (id, name, pair, normalized_pair, direction, rsi_limit,"
            " martingale_multiplier, base_size, is_active, status)"
            " VALUES (?,?,?,?,?,?,?,?,1,'IN TRADE')",
            (bot_id, f"test{bot_id}", pair, norm, "LONG", 30, 1.88, 10.0),
        )
        conn.execute(
            "INSERT INTO trades (bot_id, current_step, cycle_id, cycle_phase, open_qty,"
            " total_invested, avg_entry_price, entry_confirmed)"
            " VALUES (?, 3, 20, 'ACTIVE', ?, ?, ?, 1)",
            (bot_id, open_qty, invested, avg),
        )
        save_bot_order(bot_id, "tp", tp_oid, avg, open_qty, step=3, status="open",
                       client_order_id=tp_cid, cycle_id=20)
        # age the row past the 15s API-lag guards so cancels are not skipped
        conn.execute("UPDATE bot_orders SET created_at = ?, updated_at = ? WHERE order_id = ?",
                     (int(time.time()) - 120, int(time.time()) - 120, str(tp_oid)))
        conn.commit()

    def _row_status(self, oid):
        return get_connection().execute(
            "SELECT status FROM bot_orders WHERE order_id = ?", (str(oid),)
        ).fetchone()[0]

    def _setDown(self):
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        database.DB_PATH = self.orig_db_path
        database.backup_database = self.orig_backup
        try:
            os.remove(self.db_temp)
        except Exception:
            pass
        config.DEMO_TRADING = False
        config.TESTNET = False


class TestSweepViaMaintainOrders(_DbBacked):
    """Contract #5 (SUI site): the REAL maintain_orders stale-sweep with the
    exact SUI 10018 TP_20_3 incident state."""

    TP_OID = '179430957'
    TP_CID = f'CQB_{BOT}_TP_20_3_R1788921114'

    def setUp(self):
        config.DEMO_TRADING = True
        config.TESTNET = True
        self.reset_streaks()
        self.addCleanup(self.reset_streaks)
        self._init_db()
        self._make_bot(BOT, PAIR, "SUIUSDC", 21.1, 17.4, 0.823, self.TP_OID, self.TP_CID)
        self.ex, self.fake = self.make_exchange()
        # exchange sees the TP as NEW — the live incident shape
        self.fake.add_order(self.TP_OID, self.TP_CID, origQty='54.8', price='0.8230')

    def tearDown(self):
        self._setDown()

    def _bot_status(self):
        return {
            'cycle_id': 20, 'current_step': 3, 'open_qty': 21.1,
            'entry_confirmed': 1, 'total_invested': 17.4, 'status': 'IN TRADE',
            'cycle_phase': 'ACTIVE', 'avg_entry_price': 0.823,
            'target_tp_price': 0.8230, 'direction': 'LONG',
            'last_exit_time': 0, 'basket_start_time': int(time.time()) - 3600,
            'tp_order_id': self.TP_OID,
        }

    def _snapshot(self):
        return {'open_orders': [
            {'id': 179430957, 'clientOrderId': self.TP_CID,
             'side': 'sell', 'price': 0.823, 'amount': 54.8, 'status': 'new',
             'filled': 0.0, 'timestamp': int(time.time() * 1000)},
        ]}

    def _run_maintain(self):
        from engine.bot_executor import BotExecutor
        from engine.runner import BotRunner
        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            runner = BotRunner()
        runner.exchanges = {'future': self.ex}
        runner.exchange = self.ex
        executor = BotExecutor(runner)
        executor.maintain_orders(
            bot_id=BOT, name=f"test{BOT}", pair=PAIR, direction="LONG",
            current_price=0.82, exchange=self.ex,
            market_snapshot=self._snapshot(),
            bot_config={"market_type": "future"},
            bot_status=self._bot_status(),
        )
        return executor

    def test_liar_cancel_keeps_row_open(self):
        """SUI shape through the REAL sweep: -2011 + still-NEW order.

        RED on c3acc98: sweep ignored the result, marked the row
        'cancelling', logged "🔥 Cancelled stale" — 1127 times over 3h19m
        while the order rested live and later filled (over-sell).
        GREEN: row stays 'open', failure surfaced, streak counted.
        """
        # Operability (2026-09-11): expire any back-off armed by earlier
        # tests so THIS test exercises the raw sweep + verify-GET path.
        from engine.exchange_interface import ExchangeInterface
        ExchangeInterface._cancel_backoff_until.pop(str(self.TP_OID), None)
        self.fake.liar_ids.add(self.TP_OID)
        self._run_maintain()
        self.assertEqual(self._row_status(self.TP_OID), 'open',
            "SUI over-sell precursor: the sweep must NOT mark the row when the "
            "cancel never landed")
        self.assertEqual(self.fake.orders[self.TP_OID]['status'], 'NEW',
            "exchange order must still be live — the engine must not lose sight of it")
        from engine.exchange_interface import ExchangeInterface
        self.assertGreaterEqual(ExchangeInterface._cancel_fail_streaks.get(self.TP_OID, 0), 1)

    def test_confirmed_cancel_progresses_row(self):
        """Control (happy path): a CONFIRMED cancel still progresses the row."""
        self._run_maintain()
        self.assertIn(self._row_status(self.TP_OID), ('cancelling', 'cancelled'))
        self.assertEqual(self.fake.orders[self.TP_OID]['status'], 'CANCELED')


class TestTPSyncReplacementAborts(_DbBacked):
    """Contract #5 (XAU site): _sync_replace_tp must not mark the old TP
    cancelled nor place a replacement while the cancel is unconfirmed —
    the ghost-TP stacking shape of XAU 583851054."""

    TP_OID = '583851054'
    TP_CID = f'CQB_{BOT_XAU}_TP_14_4_R1788921085'

    def setUp(self):
        config.DEMO_TRADING = True
        config.TESTNET = True
        self.reset_streaks()
        self.addCleanup(self.reset_streaks)
        self._init_db()
        self._make_bot(BOT_XAU, XAU_PAIR, "XAUUSDT", 0.016, 69.0, 4300.0, self.TP_OID, self.TP_CID)
        self.ex, self.fake = self.make_exchange()
        self.fake.add_order(self.TP_OID, self.TP_CID, origQty='0.016', price='4330.0')

    def tearDown(self):
        self._setDown()

    def test_liar_cancel_aborts_replacement_no_ghost(self):
        """RED on c3acc98: TP-SYNC fell through on exception, marked the row
        'cancelled' (terminal) while the order stayed NEW, cleared the lock
        and placed a replacement TP → 2 live TPs → STATE-MACHINE block loop.
        GREEN: abort, row untouched, no replacement order created.
        """
        from engine.bot_executor import BotExecutor
        from engine.runner import BotRunner
        self.fake.liar_ids.add(self.TP_OID)

        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            runner = BotRunner()
        runner.exchanges = {'future': self.ex}
        runner.exchange = self.ex
        executor = BotExecutor(runner)

        result = executor._sync_replace_tp(
            bot_id=BOT_XAU, name=f"test{BOT_XAU}", pair=XAU_PAIR, direction='LONG',
            bot_status={'open_qty': 0.016, 'cycle_id': 14, 'current_step': 4},
            exchange=self.ex, db_tp=4330.0, db_qty=0.016,
            existing_tp_order={'id': self.TP_OID, 'order_id': self.TP_OID,
                               'clientOrderId': self.TP_CID, 'price': 4310.0,
                               'amount': 0.016, 'filled': 0.0},
        )
        self.assertIsNone(result, "replacement must be aborted on unconfirmed cancel")
        self.assertEqual(self._row_status(self.TP_OID), 'open',
            "the old TP row must NOT be marked cancelled while the order is live")
        self.assertEqual(self.fake.create_calls, 0,
            "no replacement TP may be placed while the old one is unconfirmed")
        self.assertEqual(self.fake.orders[self.TP_OID]['status'], 'NEW',
            "the exchange order is still live — engine must not pretend otherwise")


class TestREL1EmergencySweep(unittest.TestCase, CancelHarnessMixin):
    """Contract #6: the emergency-liquidation cancel primitive
    (cancel_orders_by_bot_id, used by shutdown.py:124 handle_emergency_liquidation)."""

    def setUp(self):
        config.DEMO_TRADING = True
        config.TESTNET = True
        self.reset_streaks()
        self.addCleanup(self.reset_streaks)

    def tearDown(self):
        config.DEMO_TRADING = False
        config.TESTNET = False

    def test_failed_cancel_does_not_count_and_sweep_continues(self):
        """One stuck (liar) order must not abort the sweep or count as cancelled.

        Emergency-path exposure (PROJECT_STATUS.md check-request 2026-09-09):
        REL-1 uses the same primitive; with the old code a raising cancel
        would abort the whole sweep at order A and never cancel order B.
        GREEN: order A stays live and uncounted; order B still gets cancelled.
        """
        ex, fake = self.make_exchange()
        fake.add_order(111, f'CQB_{BOT}_TP_20_1')
        fake.add_order(222, f'CQB_{BOT}_GRID_20_4')
        fake.liar_ids.add('111')

        cancelled = ex.cancel_orders_by_bot_id(BOT, PAIR)
        self.assertEqual(fake.orders['111']['status'], 'NEW',
            "stuck order A must still be live — the emergency path must not "
            "pretend it cancelled it")
        self.assertEqual(fake.orders['222']['status'], 'CANCELED',
            "sweep must continue past the failed order and cancel B")
        self.assertEqual(cancelled, 1,
            "only the genuinely cancelled order counts")


if __name__ == "__main__":
    unittest.main()
