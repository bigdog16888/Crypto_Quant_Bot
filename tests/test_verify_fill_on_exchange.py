import unittest
from unittest.mock import MagicMock, Mock
import sqlite3

# Import the function under test
from engine.ledger import _verify_fill_on_exchange

class TestVerifyFillOnExchange(unittest.TestCase):
    def setUp(self):
        # In‑memory DB with minimal schema needed for the function
        self.conn = sqlite3.connect(':memory:')
        cur = self.conn.cursor()
        cur.execute('CREATE TABLE bots (id INTEGER PRIMARY KEY, direction TEXT, pair TEXT)')
        cur.execute('INSERT INTO bots (id, direction, pair) VALUES (1, "LONG", "BTC/USDC:USDC")')
        self.conn.commit()
        self.bot_id = 1
        self.order_id = 'ORDER123'
        self.expected_qty = 0.1
        self.expected_price = 50000.0
        self.order_type = 'entry'

    def test_successful_fill(self):
        # Mock exchange that returns a filled order and matching trade side
        mock_ex = MagicMock()
        mock_ex.fetch_order.return_value = {'status': 'filled', 'filled': self.expected_qty}
        mock_ex.fetch_my_trades.return_value = [{
            'order': self.order_id,
            'side': 'buy'
        }]
        result = _verify_fill_on_exchange(
            conn=self.conn,
            bot_id=self.bot_id,
            order_id=self.order_id,
            expected_qty=self.expected_qty,
            expected_price=self.expected_price,
            order_type=self.order_type,
            exchange=mock_ex,
        )
        self.assertTrue(result)
        # Ensure symbol (pair) was passed correctly
        mock_ex.fetch_order.assert_called_once_with(self.order_id, 'BTC/USDC:USDC')

    def test_missing_symbol_error(self):
        # Simulate Binance 400 error when symbol omitted – raise generic Exception
        mock_ex = MagicMock()
        mock_ex.fetch_order.side_effect = Exception('Mandatory parameter \'symbol\' was not sent')
        result = _verify_fill_on_exchange(
            conn=self.conn,
            bot_id=self.bot_id,
            order_id=self.order_id,
            expected_qty=self.expected_qty,
            expected_price=self.expected_price,
            order_type=self.order_type,
            exchange=mock_ex,
        )
        self.assertFalse(result)

    def test_side_mismatch(self):
        mock_ex = MagicMock()
        mock_ex.fetch_order.return_value = {'status': 'filled', 'filled': self.expected_qty}
        # Trade side opposite to expected (LONG entry expects 'buy')
        mock_ex.fetch_my_trades.return_value = [{
            'order': self.order_id,
            'side': 'sell'
        }]
        result = _verify_fill_on_exchange(
            conn=self.conn,
            bot_id=self.bot_id,
            order_id=self.order_id,
            expected_qty=self.expected_qty,
            expected_price=self.expected_price,
            order_type=self.order_type,
            exchange=mock_ex,
        )
        self.assertFalse(result)

    def test_quantity_mismatch(self):
        mock_ex = MagicMock()
        # Qty differs by more than 1% tolerance
        mock_ex.fetch_order.return_value = {'status': 'filled', 'filled': self.expected_qty * 1.5}
        mock_ex.fetch_my_trades.return_value = [{
            'order': self.order_id,
            'side': 'buy'
        }]
        result = _verify_fill_on_exchange(
            conn=self.conn,
            bot_id=self.bot_id,
            order_id=self.order_id,
            expected_qty=self.expected_qty,
            expected_price=self.expected_price,
            order_type=self.order_type,
            exchange=mock_ex,
        )
        self.assertFalse(result)

if __name__ == '__main__':
    unittest.main()
