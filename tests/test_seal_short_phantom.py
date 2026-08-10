import os
import sys
import gc
import time
import tempfile
import pytest

from engine import database
from engine.write_queue import WriteQueue
from engine.ledger import _seal_trade_state_internal

BOT_ID = 100317
CYCLE_ID = 3

@pytest.fixture
def temp_db():
    """Create a temporary isolated SQLite database file with production schema via init_db()."""
    WriteQueue()._bypass = True
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH
    database.backup_database = lambda: None

    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    database.DB_PATH = db_path
    database.close_connection()
    database.init_db()
    database.close_connection()
    gc.collect()

    conn = database.get_connection()
    yield conn

    # Teardown
    database.close_connection()
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_short_entry_and_flatten_seal(temp_db):
    conn = temp_db
    cur = conn.cursor()
    now = int(time.time())

    # Seed bot in 'bots' table
    cur.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
    """, (BOT_ID, "long btc price_hedge", "BTC/USDC:USDC", "BTCUSDC", "SHORT", "Scanning", "{}"))

    # Seed trades row for bot
    cur.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, open_qty, cycle_id, cycle_phase, position_side, wipe_wall_ts)
        VALUES (?, 0, 0.0, 0.0, 0.0, ?, 'IDLE', 'SHORT', 0)
    """, (BOT_ID, CYCLE_ID))
    conn.commit()

    # Insert short entry fill (SELL) – 0.071 BTC @ 64823.8 USDC
    cur.execute("""
        INSERT INTO bot_orders (
            bot_id, step, order_type, order_id, client_order_id,
            price, amount, filled_amount, status, created_at, updated_at, filled_at,
            cycle_id, position_side
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        BOT_ID, 1, 'entry', '961145669', 'CQB_100317_ENTRY_3_1',
        64823.8, 0.071, 0.071, 'filled',
        now - 100, now - 100, now - 100,
        CYCLE_ID, 'SELL'
    ))

    # Insert flatten close fill (BUY) – 0.050 BTC @ 64914.6 USDC
    cur.execute("""
        INSERT INTO bot_orders (
            bot_id, step, order_type, order_id, client_order_id,
            price, amount, filled_amount, status, created_at, updated_at, filled_at,
            cycle_id, position_side
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        BOT_ID, 2, 'flatten_close', '962590689', 'CQB_100317_FLATTEN_3_0',
        64914.6, 0.050, 0.050, 'filled',
        now - 50, now - 50, now - 50,
        CYCLE_ID, 'BUY'
    ))
    conn.commit()

    # Call _seal_trade_state_internal directly (bypassing WriteQueue)
    res = _seal_trade_state_internal(BOT_ID, force_recompute=True)
    print(f"\n_seal_trade_state_internal return dict: {res}")

    # Read back trades row
    cur.execute("SELECT total_invested, open_qty, avg_entry_price, current_step FROM trades WHERE bot_id=?", (BOT_ID,))
    total_invested, open_qty, avg_price, current_step = cur.fetchone()
    print(f"trades row after seal: total_invested={total_invested:.2f}, open_qty={open_qty:.6f}, avg_entry_price={avg_price:.2f}, current_step={current_step}")

    expected_qty = 0.021
    expected_invested = round(0.021 * 64823.8, 2)  # ≈ 1361.30

    print(f"Asserting open_qty ({open_qty:.6f}) ≈ {expected_qty:.6f}")
    assert abs(open_qty - expected_qty) < 1e-5, f"Expected open_qty ≈ {expected_qty}, got {open_qty}"

    print(f"Asserting total_invested ({total_invested:.2f}) ≈ {expected_invested:.2f}")
    assert abs(total_invested - expected_invested) < 1.0, f"Expected total_invested ≈ {expected_invested}, got {total_invested}"
    assert total_invested > 0, "total_invested must be positive magnitude"
