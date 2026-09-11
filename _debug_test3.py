"""Debug the test failures directly with more tracing."""
import os
os.environ["TESTING_MODE"] = "True"
os.environ["PYTEST_RUNNING"] = "1"
import sys
import sqlite3
import time
import logging

# Force WriteQueue bypass
import engine.write_queue as wq_module
wq_module.WriteQueue._bypass = True
wq_module.WriteQueue._instance = None

from engine.database import get_connection
from engine.parity_gates import _deflate_pair_ledger_overcount_internal
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Patch at module level to be sure
orig_fetch = ExchangeInterface.fetch_order
def explode(*_args, **_kwargs):
    logger.info(f"EXPLODE CALLED with args={_args}, kwargs={_kwargs}")
    raise RuntimeError("forced exchange failure")

ExchangeInterface.fetch_order = explode

print("=== Test 1: exchange error causes reset_cleared ===")
conn = get_connection()
cur = conn.cursor()
cur.execute(
    "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, updated_at) "
    "VALUES (100317, 3, 'entry', 123456789, 0, 0, 0.0, 'open', ?, 'TEST_ORDER', 0)",
    (int(time.time()),),
)
row_id = cur.lastrowid
print(f"Inserted row_id: {row_id}")

# Verify row exists
check = conn.execute("SELECT id, status FROM bot_orders WHERE id=?", (row_id,)).fetchone()
print(f"Row check after insert: {check}")

# Call the INTERNAL function directly (bypass WriteQueue wrapper)
result = _deflate_pair_ledger_overcount_internal(
    exchange=ExchangeInterface(),
    pair="BTC/USDC:USDC",
    bot_id=100317,
    step=3,
    new_fill=0.0,
    db_id=row_id,
)
print(f"Result: {result}")

check = conn.execute("SELECT id, status, filled_amount FROM bot_orders WHERE id=?", (row_id,)).fetchone()
print(f"Row after: {check}")

conn.close()