"""
test_credit_fill_race_retry.py

Regression test for the credit_fill race window in ws_event_handlers.py.

Scenario: A taker order fills on exchange instantly. The WS FILL event arrives
before BotExecutor's save_bot_order() has committed the bot_orders row. The old
code called _attribute_orphan_fill() which — with ALLOW_FORENSIC_ADOPT=False —
silently dropped the fill, leaving the ledger understating the position by the
fill quantity.

The fix: a _pending_fills dict keyed by order_id retains the fill payload with
a retry counter. On each subsequent call to _drain_pending_fills() (invoked at
the top of the next WS cycle), the engine retries credit_fill up to
PENDING_FILL_MAX_RETRIES times (default 3, ~3 s). If still unresolved after the
final retry, the bot is flagged REQUIRE_MANUAL_PROOF — no silent drop.

Test cases:
  1. test_fill_enqueued_when_row_missing
     WS FILL arrives → no DB row → fill is put in _pending_fills.
  2. test_retry_succeeds_when_row_appears
     First attempt: row missing → enqueued. Second attempt: row exists →
     credit_fill succeeds → _pending_fills cleared.
  3. test_escalate_after_max_retries
     Row never appears. After PENDING_FILL_MAX_RETRIES exhausted →
     flag_orphan_fill_manual_proof called, fill removed from queue.
  4. test_no_duplicate_credit_on_success
     credit_fill is called exactly once after the row appears.
  5. test_forensic_adopt_bypassed
     ALLOW_FORENSIC_ADOPT=False must NOT affect retry behaviour — retries are
     proof-based (real order_id), not forensic invention.
"""
import sys
import os
import time
import threading
import unittest
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Helpers — build a minimal WS event payload
# ---------------------------------------------------------------------------
def _make_fill_event(bot_id: int, order_id: str, client_id: str,
                     qty: float = 0.002, price: float = 76816.1,
                     symbol: str = 'BTCUSDC', order_type: str = 'ENTRY') -> dict:
    return {
        'event': 'order_update',
        'symbol': symbol,
        'side': 'SELL',
        'status': 'FILLED',
        'order_id': order_id,
        'client_order_id': client_id,
        'price': price,
        'qty': qty,
        'filled_qty': qty,
        'avg_price': price,
        'realized_pnl': 0.0,
        'timestamp': int(time.time() * 1000),
        'lastTradeTimestamp': int(time.time() * 1000),
        # Parsed by handle_order_update:
        '_bot_id': bot_id,
        '_order_type': order_type,
    }


class TestCreditFillRaceRetry(unittest.TestCase):

    def setUp(self):
        """
        Import the ws_event_handlers module with a fresh _pending_fills dict
        on every test so tests are isolated.
        """
        # Remove any cached version so we get a clean module state per test
        for mod_name in list(sys.modules.keys()):
            if 'ws_event_handlers' in mod_name:
                del sys.modules[mod_name]

        import engine.ws_event_handlers as wseh
        self.wseh = wseh

        # Reset module-level state
        wseh._pending_fills.clear()

        self.BOT_ID = 10022
        self.ORDER_ID = '348125134'
        self.CLIENT_ID = f'CQB_{self.BOT_ID}_ENTRY_4_1'
        self.SYMBOL = 'BTCUSDC'
        self.QTY = 0.002
        self.PRICE = 76816.1

    # ------------------------------------------------------------------
    # Test 1 — fill is enqueued when bot_orders row doesn't exist yet
    # ------------------------------------------------------------------
    def test_fill_enqueued_when_row_missing(self):
        """
        When credit_fill returns False (row missing), the fill payload must be
        placed in _pending_fills keyed by order_id.
        """
        with patch.object(self.wseh, '_credit_fill_with_retry') as mock_credit, \
             patch.object(self.wseh, '_enqueue_db_write') as mock_enqueue:

            mock_credit.return_value = False  # Simulate row not yet in DB

            self.wseh._handle_fill_with_pending_retry(
                bot_id=self.BOT_ID,
                order_id=self.ORDER_ID,
                client_id=self.CLIENT_ID,
                qty=self.QTY,
                price=self.PRICE,
                order_type='entry',
                fill_ts=int(time.time()),
                symbol=self.SYMBOL,
            )

        self.assertIn(self.ORDER_ID, self.wseh._pending_fills,
                      "Fill should be in _pending_fills after credit_fill miss")
        entry = self.wseh._pending_fills[self.ORDER_ID]
        self.assertEqual(entry['bot_id'], self.BOT_ID)
        self.assertEqual(entry['qty'], self.QTY)
        self.assertEqual(entry['retries'], 0)

    # ------------------------------------------------------------------
    # Test 2 — retry succeeds when the DB row appears on second attempt
    # ------------------------------------------------------------------
    def test_retry_succeeds_when_row_appears(self):
        """
        Enqueue a pending fill manually. On first drain the row is still
        missing (credit_fill → False for all calls in that drain). On second
        drain the row exists → credit_fill succeeds → _pending_fills cleared
        and seal_trade_state enqueued.

        Note: _credit_fill_with_retry calls credit_fill up to TWICE per drain
        attempt (order_id first, then client_id fallback). We track drain
        cycles, not raw call count, so the mock uses a drain counter.
        """
        from engine.ws_event_handlers import (
            _pending_fills, PENDING_FILL_MAX_RETRIES
        )

        now = int(time.time())
        _pending_fills[self.ORDER_ID] = {
            'bot_id': self.BOT_ID,
            'client_id': self.CLIENT_ID,
            'qty': self.QTY,
            'price': self.PRICE,
            'order_type': 'entry',
            'fill_ts': now,
            'symbol': self.SYMBOL,
            'retries': 0,
            'first_seen': now,
        }

        drain_count = {'n': 0}

        def credit_side_effect(*args, **kwargs):
            """
            Return False for all calls during drain #1; True from drain #2 on.
            We detect drain boundaries via the retry counter in _pending_fills.
            """
            current_retries = _pending_fills.get(self.ORDER_ID, {}).get('retries', 0)
            # During drain #1 retries is still 0 (not yet incremented) → fail
            if current_retries == 0:
                return False
            # drain #2+ retries >= 1 → succeed
            return True

        seal_mock = MagicMock()

        with patch('engine.ledger.credit_fill', side_effect=credit_side_effect), \
             patch('engine.ledger.seal_trade_state', seal_mock), \
             patch.object(self.wseh, '_enqueue_db_write',
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):

            # First drain — credit_fill still fails, retries incremented to 1
            self.wseh._drain_pending_fills()
            self.assertIn(self.ORDER_ID, _pending_fills,
                          "Should still be pending after first failed retry")
            self.assertEqual(_pending_fills[self.ORDER_ID]['retries'], 1)

            # Second drain — credit_fill now succeeds, fill removed from queue
            self.wseh._drain_pending_fills()

        self.assertNotIn(self.ORDER_ID, _pending_fills,
                         "Should be removed from _pending_fills after successful retry")
        seal_mock.assert_called_once_with(self.BOT_ID)

    # ------------------------------------------------------------------
    # Test 3 — escalate to REQUIRE_MANUAL_PROOF after max retries
    # ------------------------------------------------------------------
    def test_escalate_after_max_retries(self):
        """
        If the row never appears, after PENDING_FILL_MAX_RETRIES attempts the
        fill must be escalated to flag_orphan_fill_manual_proof and removed
        from _pending_fills — not silently dropped.
        """
        from engine.ws_event_handlers import (
            _pending_fills, PENDING_FILL_MAX_RETRIES
        )

        now = int(time.time())
        _pending_fills[self.ORDER_ID] = {
            'bot_id': self.BOT_ID,
            'client_id': self.CLIENT_ID,
            'qty': self.QTY,
            'price': self.PRICE,
            'order_type': 'entry',
            'fill_ts': now,
            'symbol': self.SYMBOL,
            'retries': 0,
            'first_seen': now,
        }

        flag_mock = MagicMock()

        with patch('engine.ledger.credit_fill', return_value=False), \
             patch('engine.parity_gates.flag_orphan_fill_manual_proof', flag_mock), \
             patch.object(self.wseh, '_enqueue_db_write',
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):

            # Drain MAX_RETRIES times to exhaust the counter (each drain increments retries)
            for _ in range(PENDING_FILL_MAX_RETRIES):
                self.wseh._drain_pending_fills()

            # One final drain: now retries == MAX_RETRIES → escalation fires
            self.wseh._drain_pending_fills()

        self.assertNotIn(self.ORDER_ID, _pending_fills,
                         "Fill must be removed from queue after max retries exhausted")
        flag_mock.assert_called_once()
        args = flag_mock.call_args[0]
        self.assertEqual(args[0], self.BOT_ID)
        self.assertEqual(args[1], self.ORDER_ID)

    # ------------------------------------------------------------------
    # Test 4 — no double credit if fill succeeds on first retry
    # ------------------------------------------------------------------
    def test_no_duplicate_credit_on_success(self):
        """
        credit_fill must be called exactly once after the row appears.
        """
        from engine.ws_event_handlers import _pending_fills

        now = int(time.time())
        _pending_fills[self.ORDER_ID] = {
            'bot_id': self.BOT_ID,
            'client_id': self.CLIENT_ID,
            'qty': self.QTY,
            'price': self.PRICE,
            'order_type': 'entry',
            'fill_ts': now,
            'symbol': self.SYMBOL,
            'retries': 0,
            'first_seen': now,
        }

        credit_mock = MagicMock(return_value=True)

        with patch('engine.ledger.credit_fill', credit_mock), \
             patch('engine.ledger.seal_trade_state', MagicMock()), \
             patch.object(self.wseh, '_enqueue_db_write',
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):

            # Drain twice — second should be a no-op (fill already removed)
            self.wseh._drain_pending_fills()
            self.wseh._drain_pending_fills()

        self.assertEqual(credit_mock.call_count, 1,
                         "credit_fill must be called exactly once")

    # ------------------------------------------------------------------
    # Test 5 — ALLOW_FORENSIC_ADOPT=False must not suppress retries
    # ------------------------------------------------------------------
    def test_forensic_adopt_bypassed(self):
        """
        The retry mechanism is proof-based and must operate independently of
        ALLOW_FORENSIC_ADOPT. Setting it False must not prevent the retry
        from crediting a legitimate fill once the DB row appears.
        """
        from engine.ws_event_handlers import _pending_fills

        now = int(time.time())
        _pending_fills[self.ORDER_ID] = {
            'bot_id': self.BOT_ID,
            'client_id': self.CLIENT_ID,
            'qty': self.QTY,
            'price': self.PRICE,
            'order_type': 'entry',
            'fill_ts': now,
            'symbol': self.SYMBOL,
            'retries': 0,
            'first_seen': now,
        }

        credit_mock = MagicMock(return_value=True)
        seal_mock = MagicMock()

        with patch('engine.ledger.credit_fill', credit_mock), \
             patch('engine.ledger.seal_trade_state', seal_mock), \
             patch('engine.parity_gates.forensic_adopt_allowed', return_value=False), \
             patch.object(self.wseh, '_enqueue_db_write',
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):

            self.wseh._drain_pending_fills()

        self.assertNotIn(self.ORDER_ID, _pending_fills,
                         "Fill should succeed via retry regardless of ALLOW_FORENSIC_ADOPT")
        credit_mock.assert_called_once()
        seal_mock.assert_called_once_with(self.BOT_ID)


if __name__ == '__main__':
    unittest.main(verbosity=2)
