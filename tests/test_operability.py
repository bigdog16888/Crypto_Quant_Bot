"""Scenario + unit tests: OPERABILITY session (feat/operability, 2026-09-10).

Scope confirmed by operator (one branch, two-gate):
  1. Alert channel — CRITICAL/ERROR engine records route to Telegram push +
     DB notifications table; no channel configured -> push disabled, zero
     network calls; alerting must NEVER raise into the logging path.
  2. Per-(order) dedup on alerting — live evidence: 24 CANCEL-ESCALATION
     CRITICALs in 30 min on order 583851054 (2026-09-10). Contract: one push
     per alert key per dedup window, never per-log-line.
  3. Dead-man heartbeat — engine writes a heartbeat file each main-loop
     iteration; ops.check_heartbeat() flags staleness. ALERT-ONLY: nothing
     in the engine auto-restarts it (operator doctrine: restarts only from
     a single fully-tested branch at a named hash).
  4. DB backup — sqlite online-backup API (engine/ops.backup_database),
     called on graceful shutdown; restore refuses to overwrite a NEWER live
     DB with an older backup.
  5. Clock re-sync — the one-shot _sync_time_offset becomes periodic
     (CLOCK_RESYNC_INTERVAL); an elapsed interval re-syncs and logs it.
  6. Per-order cancel back-off — the e68e9e1 fix's retry loop is itself a
     rate-limit hazard (docs/RATE_LIMIT_WEIGHT_MAP.md sec.4: N stuck orders
     x (DELETE+verify-GET)/min scales toward the 1200 orders/min cap).
     Back-off goes exponential per order id once the failure streak reaches
     CANCEL_STREAK_ESCALATION, capped at CANCEL_BACKOFF_MAX_SECONDS (30s);
     the emergency sweep (cancel_orders_by_bot_id) BYPASSES back-off via
     force=True (position closing must never be starved).
  7. Weight/429 governance — _raw_request reads X-MBX-USED-WEIGHT-1M from
     every response into the rate governor; sustained >RATE_LIMIT_SOFT_PCT
     of the weight cap logs+alerts once (deduped); HTTP 429 (or 400 -1003)
     raises RateLimitError carrying the raw body, always logged.

RED on 7a5948c: contracts 1-5 and 7 fail on missing modules/exceptions;
contract 6 fails live (4th cancel still hits the exchange — no back-off);
contract 5 fails live (re-sync never happens after the first sync).
"""
import json
import os
import sys
import time
import logging
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import init_db, get_connection

BOT = 93018          # test-only id (90001+ range, outside STARTUP_EXCLUDED_BOT_IDS)
PAIR = "XAU/USDT:USDT"


# ----------------------------------------------------------------------
# Harness — same seam as the proven tests/test_silent_cancel_swallow.py:
# the REAL ExchangeInterface with a fake Demo FAPI behind requests.request.
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


def make_fake(liar_ids=None, orders=None):
    class FakeDemoFAPI:
        def __init__(self):
            self.orders = dict(orders or {})   # order_id -> raw FAPI dict
            # Normalize to strings: orderIds arrive as strings from parsed
            # query params — an int set never matches and every "liar"
            # cancel silently succeeds instead (found via GREEN run 3).
            self.liar_ids = {str(x) for x in (liar_ids or [])}
            self.delete_calls = 0
            self.get_calls = 0

        def _live(self, oid):
            o = self.orders.get(str(oid))
            return o is not None and o['status'] in ('NEW', 'PARTIALLY_FILLED')

        def _raw(self, endpoint, method, params):
            oid = params.get('orderId')
            if endpoint == '/fapi/v1/order' and method == 'DELETE':
                self.delete_calls += 1
                if not self._live(oid):
                    return FakeResponse(400, '{"code":-2011,"msg":"Unknown order sent."}')
                if oid in self.liar_ids:
                    return FakeResponse(400, '{"code":-2011,"msg":"Unknown order sent."}')
                o = self.orders[str(oid)]
                o['status'] = 'CANCELED'
                return FakeResponse(200, json.dumps(o))
            if endpoint == '/fapi/v1/order' and method == 'GET':
                self.get_calls += 1
                if str(oid) not in self.orders:
                    return FakeResponse(400, '{"code":-2013,"msg":"Order does not exist."}')
                return FakeResponse(200, json.dumps(self.orders[str(oid)]))
            # Construction probes and anything else: benign empty answers.
            if method == 'GET':
                return FakeResponse(200, '[]')
            return FakeResponse(200, '{}')

        def __call__(self, method, url, **kwargs):
            from urllib.parse import urlparse, parse_qsl
            q = dict(parse_qsl(urlparse(url).query))
            ep = urlparse(url).path
            return self._raw(ep, method, q)

    return FakeDemoFAPI()


def make_ex(liar_ids=None, orders=None, preserve_clock=False):
    """Real ExchangeInterface over the fake; suppresses network at build.

    The requests.request patch stays ACTIVE for the whole test (stopped in
    TempDB.tearDown) — otherwise every post-construction exchange call leaks
    to the real network (live evidence: 401 -2014 from the empty worktree
    key while the fake's call counters stayed at 0).
    """
    import engine.exchange_interface as ei
    # Skip the one-shot time sync (class attr set) so no real requests.get fires.
    ei.ExchangeInterface._time_offset = 0
    if not preserve_clock:
        ei.ExchangeInterface._last_offset_sync = time.time()
    fake = make_fake(liar_ids, orders)
    patcher = patch.object(ei.requests, 'request', side_effect=fake)
    patcher.start()
    _ACTIVE_PATCHES.append(patcher)
    ex = ei.ExchangeInterface(market_type='future')
    return ex, fake


_ACTIVE_PATCHES = []


def stop_all_patches():
    while _ACTIVE_PATCHES:
        try:
            _ACTIVE_PATCHES.pop().stop()
        except Exception:
            pass


def reset_cancel_state():
    import engine.exchange_interface as ei
    ei.ExchangeInterface._cancel_fail_streaks.clear()
    if hasattr(ei.ExchangeInterface, '_cancel_backoff_until'):
        ei.ExchangeInterface._cancel_backoff_until.clear()


class LogCapture(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append((record.levelname, record.getMessage()))


class TempDB(unittest.TestCase):
    """Isolated temp crypto_bot.db per test (Windows file locks, etc.)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, 'crypto_bot.db')
        database.DB_PATH = self.db_path
        os.environ['DEMO_TRADING'] = 'True'
        os.environ['TESTNET'] = 'True'
        init_db()
        self.log_msgs = []
        self.cap = LogCapture(self.log_msgs)
        self.cap.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(self.cap)
        root.setLevel(logging.DEBUG)
        # Silence the ccxt/urllib3 DEBUG flood (exchange construction during
        # tests dumps ~MBs of market JSON otherwise).
        self._saved_levels = {}
        for noisy in ('ccxt', 'urllib3', 'web3', 'asyncio'):
            self._saved_levels[noisy] = logging.getLogger(noisy).level
            logging.getLogger(noisy).setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger().removeHandler(self.cap)
        for noisy, lvl in self._saved_levels.items():
            logging.getLogger(noisy).setLevel(lvl)
        stop_all_patches()
        # Close the cached thread-local DB connection BEFORE deleting the
        # tmpdir — an open handle holds the Windows file lock (WinError 32)
        # and turns every test into a teardown error.
        try:
            if getattr(database._local, 'connection', None) is not None:
                database._local.connection.close()
                database._local.connection = None
        except Exception:
            pass
        database.DB_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'crypto_bot.db')
        self.tmpdir.cleanup()
        reset_cancel_state()


# ======================================================================
# Contract 6 — per-order cancel back-off (RED-able on existing code)
# ======================================================================

class TestCancelBackoff(TempDB):
    """Back-off arms after CANCEL_STREAK_ESCALATION consecutive failures."""

    def _liar_ex(self):
        return make_ex(liar_ids=[583851054],
                      orders={'583851054': {'orderId': 583851054, 'status': 'NEW',
                                             'symbol': 'XAUUSDT', 'executedQty': '0'}})

    def test_backoff_gate_blocks_immediate_retry(self):
        """Live XAU shape: DELETE -2011 on a live order, verify-GET shows new.

        After the escalation threshold fires, the next cancel within the
        back-off window must NOT reach the exchange (no DELETE, no verify-GET)
        — it raises CancelFailedError('back-off') immediately.
        """
        from engine.exceptions import CancelFailedError
        ex, fake = self._liar_ex()
        reset_cancel_state()
        for _ in range(3):                      # arm the streak
            with self.assertRaises(CancelFailedError):
                ex.cancel_order(583851054, PAIR)
        self.assertTrue(any('CANCEL-ESCALATION' in m for _, m in self.log_msgs),
                        "escalation must fire before back-off arms")
        calls_before = fake.delete_calls + fake.get_calls
        with self.assertRaises(CancelFailedError) as cm:
            ex.cancel_order(583851054, PAIR)
        self.assertIn('back-off', str(cm.exception),
                      "gated cancel must say it was deferred by back-off")
        self.assertEqual(fake.delete_calls + fake.get_calls, calls_before,
                         "a back-off'd cancel must NOT hit the exchange")

    def test_backoff_expires_and_allows_retry(self):
        """Once the window lapses, a cancel is a REAL retry (hits exchange)."""
        from engine.exceptions import CancelFailedError
        ex, fake = self._liar_ex()
        reset_cancel_state()
        for _ in range(3):
            with self.assertRaises(CancelFailedError):
                ex.cancel_order(583851054, PAIR)
        # Expire the armed window.
        ex._cancel_backoff_until[str(583851054)] = 0.0
        calls_before = fake.delete_calls + fake.get_calls
        with self.assertRaises(CancelFailedError):
            ex.cancel_order(583851054, PAIR)
        self.assertGreater(fake.delete_calls + fake.get_calls, calls_before,
                           "expired back-off must allow a real retry")

    def test_emergency_sweep_bypasses_backoff(self):
        """REL-1 path: cancel_orders_by_bot_id must cancel even when the
        order is in back-off (position closing is never starved)."""
        from engine.exceptions import CancelFailedError
        ex, fake = self._liar_ex()
        reset_cancel_state()
        for _ in range(3):
            with self.assertRaises(CancelFailedError):
                ex.cancel_order(583851054, PAIR)
        # Order is now in back-off; single cancels are gated…
        calls_before = fake.delete_calls
        with self.assertRaises(CancelFailedError):
            ex.cancel_order(583851054, PAIR)
        self.assertEqual(fake.delete_calls, calls_before)
        # …but the emergency sweep still issues its DELETE.
        ex.cancel_orders_by_bot_id(93018, PAIR)   # prefix CQB_93018_ — no match is fine
        # The sweep cancelled nothing matching (clientOrderId prefix differs),
        # so prove bypass directly: sweep on a bot whose open order IS the
        # stuck one is covered by fetch_open_orders plumbing; the bypass
        # contract is that force=True reaches the exchange:
        with patch.object(ex, 'fetch_open_orders', return_value=[
            {'id': 583851054, 'clientOrderId': 'CQB_93018_TP_14_4_R1',
             'symbol': 'XAU/USDT:USDT', 'status': 'open', 'filled': 0}]):
            ex.cancel_orders_by_bot_id(93018, PAIR)
        self.assertGreater(fake.delete_calls, calls_before,
                           "emergency sweep must bypass back-off and issue the DELETE")


# ======================================================================
# Contract 7 — weight/429 governance
# ======================================================================

class TestRateGovernance(TempDB):

    def test_429_raises_typed_rate_limit_error(self):
        """Live-failure shape: 429 with -1003 body must raise RateLimitError
        carrying the raw body (never collapse, never mask as generic)."""
        from engine.exceptions import RateLimitError
        import engine.exchange_interface as ei
        ex, _ = make_ex()

        def fake429(method, url, **kw):
            return FakeResponse(429, '{"code":-1003,"msg":"Too many requests."}',
                               headers={"Retry-After": "5"})

        with patch.object(ei.requests, 'request', side_effect=fake429):
            with self.assertRaises(RateLimitError) as cm:
                ex._raw_request('/fapi/v1/order', method='GET',
                                params={'symbol': 'XAUUSDT', 'orderId': 583851054})
        self.assertIn('-1003', str(cm.exception))
        self.assertIn('-1003', getattr(cm.exception, 'raw_body', ''))
        self.assertTrue(any('429' in m for _, m in self.log_msgs),
                        "the raw 429 body must always be logged")

    def test_weight_header_tracked_into_governor(self):
        """Every response's X-MBX-USED-WEIGHT-1M feeds the governor."""
        import engine.exchange_interface as ei
        from engine import rate_governor
        ex, _ = make_ex()
        seq = [FakeResponse(200, '[]', headers={"X-MBX-USED-WEIGHT-1M": "2300"}),
               FakeResponse(200, '[]', headers={"X-MBX-USED-WEIGHT-1M": "120"})]
        with patch.object(ei.requests, 'request', side_effect=lambda m, u, **k: seq.pop(0)):
            ex._raw_request('/fapi/v1/openOrders', method='GET', params={'symbol': 'XAUUSDT'})
            ex._raw_request('/fapi/v1/openOrders', method='GET', params={'symbol': 'XAUUSDT'})
        self.assertEqual(rate_governor.governor.last_weight, 120)

    def test_sustained_high_weight_alerts_once(self):
        """3 consecutive responses over 80% of the cap -> ONE governing
        alert (deduped), not one per response."""
        import engine.exchange_interface as ei
        from engine import rate_governor
        ex, _ = make_ex()
        hot = [FakeResponse(200, '[]', headers={"X-MBX-USED-WEIGHT-1M": "2300"})] * 5
        with patch.object(ei.requests, 'request', side_effect=lambda m, u, **k: hot.pop(0)):
            for _ in range(5):
                ex._raw_request('/fapi/v1/openOrders', method='GET',
                                params={'symbol': 'XAUUSDT'})
        gov_alerts = [m for lvl, m in self.log_msgs if 'RATE-GOVERNOR' in m]
        self.assertEqual(len(gov_alerts), 1,
                         f"sustained-high must alert exactly once, got {gov_alerts}")

    def test_governor_throttle_decision(self):
        """should_throttle() is True above the soft threshold, False below."""
        from engine import rate_governor
        rg = rate_governor.RateGovernor(limit=2400, soft_pct=0.8)
        rg.note_weight(2300)     # 95.8%
        self.assertTrue(rg.should_throttle())
        rg.note_weight(100)      # 4%
        self.assertFalse(rg.should_throttle())


# ======================================================================
# Contracts 1+2 — alert channel + per-order dedup
# ======================================================================

class TestAlertRouter(TempDB):

    def _router(self, **cfg):
        from engine import alerting
        return alerting.AlertRouter(token=cfg.get('token', 'TEST-TOKEN'),
                                    chat_id=cfg.get('chat_id', '123'))

    def test_escalation_flood_pushes_once_per_order(self):
        """24 CANCEL-ESCALATION CRITICALs on one order in 30 min -> ONE push."""
        router = self._router()
        pushes = []
        with patch.object(router, '_send_telegram', side_effect=lambda msg: pushes.append(msg)):
            for streak in range(3, 1040, 43):   # 24 escalations, same order
                router.push(logging.LogRecord(
                    'ExchangeInterface', logging.CRITICAL, __file__, 1,
                    f"🚨 [CANCEL-ESCALATION] Order 583851054: {streak} consecutive "
                    f"cancel attempts FAILED without confirmation.", None, None))
        self.assertEqual(len(pushes), 1,
                         f"24 escalation lines on one order must push once, got {len(pushes)}")

    def test_distinct_orders_each_push(self):
        """Two different stuck orders -> two pushes (dedup key is the order)."""
        router = self._router()
        pushes = []
        with patch.object(router, '_send_telegram', side_effect=lambda msg: pushes.append(msg)):
            for oid in (583851054, 583851055):
                for _ in range(3):
                    router.push(logging.LogRecord(
                        'ExchangeInterface', logging.CRITICAL, __file__, 1,
                        f"🚨 [CANCEL-ESCALATION] Order {oid}: 30 consecutive "
                        f"cancel attempts FAILED.", None, None))
        self.assertEqual(len(pushes), 2)

    def test_info_records_never_push(self):
        router = self._router()
        pushes = []
        with patch.object(router, '_send_telegram', side_effect=lambda msg: pushes.append(msg)):
            router.push(logging.LogRecord(
                'BotRunner', logging.INFO, __file__, 1,
                "✅ Active Positions Synced: 5", None, None))
            router.push(logging.LogRecord(
                'BotRunner', logging.WARNING, __file__, 1,
                "⚠️ minor warning", None, None))
        self.assertEqual(pushes, [])

    def test_no_channel_configured_means_zero_network(self):
        """Empty token -> hard-off: no telegram attempt, but DB notification
        row still lands (the UI channel stays usable)."""
        from engine import alerting
        router = alerting.AlertRouter(token='', chat_id='')
        posts = []
        with patch.object(alerting.requests, 'post', side_effect=lambda *a, **k: posts.append(a)):
            router.push(logging.LogRecord(
                'ExchangeInterface', logging.CRITICAL, __file__, 1,
                "🚨 [CANCEL-ESCALATION] Order 583851054: 3 consecutive.", None, None))
        self.assertEqual(posts, [], "no token configured must mean zero network calls")
        rows = get_connection().execute(
            "SELECT COUNT(*) FROM notifications WHERE type='alert'").fetchone()[0]
        self.assertEqual(rows, 1, "DB notification row must still be written")

    def test_alert_failure_never_raises(self):
        """A broken push channel must not propagate into the logging path."""
        from engine import alerting
        router = alerting.AlertRouter(token='T', chat_id='1')
        with patch.object(router, '_send_telegram', side_effect=RuntimeError("boom")):
            try:
                router.push(logging.LogRecord(
                    'X', logging.CRITICAL, __file__, 1, "🚨 bad channel", None, None))
            except Exception as e:
                self.fail(f"router.push must swallow channel errors, raised {e}")


# ======================================================================
# Contract 3 — dead-man heartbeat (alert-only)
# ======================================================================

class TestHeartbeat(TempDB):

    def test_write_and_fresh(self):
        from engine import ops
        ops.write_heartbeat()
        self.assertLess(ops.heartbeat_age_seconds(), 5)
        self.assertFalse(ops.check_heartbeat(stale_seconds=300))

    def test_stale_heartbeat_flags(self):
        from engine import ops
        hb = os.path.join(os.path.dirname(database.DB_PATH), 'engine.heartbeat')
        with open(hb, 'w') as f:
            f.write(str(int(time.time()) - 3600))     # 1h old
        self.assertTrue(ops.check_heartbeat(stale_seconds=300))

    def test_no_heartbeat_file_flags_stale(self):
        from engine import ops
        hb = os.path.join(os.path.dirname(database.DB_PATH), 'engine.heartbeat')
        if os.path.exists(hb):
            os.remove(hb)
        self.assertTrue(ops.check_heartbeat(stale_seconds=300))


# ======================================================================
# Contract 4 — DB backup (online-backup API + restore guard)
# ======================================================================

class TestBackup(TempDB):

    def test_backup_is_valid_sqlite_copy(self):
        from engine import ops
        get_connection().execute(
            "INSERT INTO notifications (timestamp, type, message, bot_id, is_read) "
            "VALUES (?, 'alert', 'backup-probe', ?, 0)", (int(time.time()), BOT))
        get_connection().commit()
        path = ops.backup_database(dest_dir=self.tmpdir.name)
        self.assertTrue(path and os.path.exists(path))
        probe = sqlite3.connect(path)
        rows = probe.execute(
            "SELECT COUNT(*) FROM notifications WHERE message='backup-probe'").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(probe.execute("PRAGMA integrity_check").fetchone()[0], 'ok')
        probe.close()

    def test_restore_refuses_over_newer_live_db(self):
        from engine import ops
        path = ops.backup_database(dest_dir=self.tmpdir.name)
        time.sleep(1.1)      # live DB is now strictly newer than the backup
        with self.assertRaises(RuntimeError):
            ops.restore_database(path)


# ======================================================================
# Contract 5 — periodic clock re-sync
# ======================================================================

class TestClockResync(TempDB):

    def test_resync_after_interval(self):
        """The one-shot sync (7a5948c) never re-syncs. Periodic contract:
        once CLOCK_RESYNC_INTERVAL has elapsed, a sync fires and logs."""
        import engine.exchange_interface as ei
        ei.ExchangeInterface._time_offset = 0
        ei.ExchangeInterface._last_offset_sync = time.time() - 7200   # 2h stale

        calls = []
        class FakeTimeResp:
            status_code = 200
            def json(self):
                return {'serverTime': int(time.time() * 1000) + 123}

        with patch.object(ei.requests, 'get', side_effect=lambda *a, **k: (calls.append(a), FakeTimeResp())[1]):
            ex, _ = make_ex(preserve_clock=True)
            ex._sync_time_offset()
        self.assertEqual(len(calls), 1, "stale interval must trigger a re-sync fetch")
        self.assertTrue(any('re-sync' in m.lower() for _, m in self.log_msgs),
                        "re-sync must be logged")

    def test_no_resync_within_interval(self):
        import engine.exchange_interface as ei
        ei.ExchangeInterface._time_offset = 0
        ei.ExchangeInterface._last_offset_sync = time.time()   # just synced
        calls = []
        with patch.object(ei.requests, 'get', side_effect=lambda *a, **k: (calls.append(a), None)[1]):
            ex, _ = make_ex()
            ex._sync_time_offset()
        self.assertEqual(len(calls), 0, "fresh sync must NOT refetch")


if __name__ == '__main__':
    unittest.main()
