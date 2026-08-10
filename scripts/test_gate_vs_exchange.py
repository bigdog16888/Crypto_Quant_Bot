#!/usr/bin/env python3
"""
Test _verify_fill_on_exchange (FIXED) against real Binance testnet.
"""
import os, sys
os.environ.pop('PYTHONPATH', None)

sys.path.insert(0, r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')

from engine.exchange_interface import ExchangeInterface
from engine.ledger import _verify_fill_on_exchange
from engine import database

database.DB_PATH = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
database.close_connection()

conn = database.get_connection()
exchange = ExchangeInterface(market_type='future')

print("=" * 70)
print("TEST 1: Real order 961145669 (100317 SHORT entry, expect side=sell)")
print("=" * 70)

result = _verify_fill_on_exchange(
    conn=conn, bot_id=100317, order_id='961145669',
    expected_qty=0.071, expected_price=64823.8,
    order_type='entry', exchange=exchange
)
print(f"Result: {result}  (expected: True)")

print()
print("=" * 70)
print("TEST 2: Real order 958355137 (10016 LONG entry, expect side=buy)")
print("=" * 70)

result2 = _verify_fill_on_exchange(
    conn=conn, bot_id=10016, order_id='958355137',
    expected_qty=0.002, expected_price=65258.4,
    order_type='entry', exchange=exchange
)
print(f"Result: {result2}  (expected: True)")

print()
print("=" * 70)
print("TEST 3: Wrong qty (should fail)")
print("=" * 70)

result3 = _verify_fill_on_exchange(
    conn=conn, bot_id=100317, order_id='961145669',
    expected_qty=99.0, expected_price=64823.8,
    order_type='entry', exchange=exchange
)
print(f"Result: {result3}  (expected: False)")

print()
print("=" * 70)
print("TEST 4: Wrong side (LONG bot with SHORT order — should fail)")
print("=" * 70)

result4 = _verify_fill_on_exchange(
    conn=conn, bot_id=10016, order_id='961145669',
    expected_qty=0.071, expected_price=64823.8,
    order_type='entry', exchange=exchange
)
print(f"Result: {result4}  (expected: False — 10016 is LONG, order is sell)")

print()
print("=" * 70)
print("TEST 5: Ghost order (nonexistent)")
print("=" * 70)

result5 = _verify_fill_on_exchange(
    conn=conn, bot_id=100317, order_id='999999999999',
    expected_qty=0.071, expected_price=64823.8,
    order_type='entry', exchange=exchange
)
print(f"Result: {result5}  (expected: False)")

print()
print("=" * 70)
print("TEST 6: No exchange (should fail closed)")
print("=" * 70)

result6 = _verify_fill_on_exchange(
    conn=conn, bot_id=100317, order_id='961145669',
    expected_qty=0.071, expected_price=64823.8,
    order_type='entry', exchange=None
)
print(f"Result: {result6}  (expected: False)")

database.close_connection()
