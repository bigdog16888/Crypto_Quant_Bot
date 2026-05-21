"""Test that UI suppresses MISSING GRIDS alert when one-way netting opposite entry is blocked."""
import pytest
import sqlite3
import uuid
import pandas as pd
from engine import database
from engine import oneway_netting

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_ui_netting_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    yield database.get_connection()
    sqlite3.connect = orig_connect
    database.DB_PATH = orig_db_path
    database.backup_database = orig_backup
    persistent_conn.close()


def _seed_bot(conn, bot_id, name, pair, direction, open_qty=0.0, cycle=1, current_step=0, total_invested=0.0):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')",
        (bot_id, name, pair, 'BTCUSDC', direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side, current_step, total_invested) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        (bot_id, cycle, open_qty, direction, current_step, total_invested),
    )
    conn.commit()


def test_ui_missing_grids_suppression(memory_db):
    # Seed a LONG bot with actual open position
    _seed_bot(memory_db, 10016, "Bot_LONG", 'BTC/USDC:USDC', 'LONG', open_qty=0.008, total_invested=100.0, current_step=1)
    
    # Seed a SHORT bot mid-cycle but with 0 open position (entry blocked or waiting)
    # This bot should trigger MISSING GRIDS because actual_ph < 2, c_step >= 1, and bot_inv > 0.01
    _seed_bot(memory_db, 10022, "Bot_SHORT", 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, total_invested=50.0, current_step=1)

    # 1. Verify that gate_oneway_opposite_entry blocks the SHORT bot due to opposite LONG position
    ok, reason = oneway_netting.gate_oneway_opposite_entry(10022, 'BTC/USDC:USDC', 'SHORT')
    assert not ok, f"Expected SHORT bot to be blocked, but got: {ok}"

    # 2. Simulate the UI alert logic in ui/views/monitor.py
    # We will build a dummy df_pos_f representing our SHORT bot
    df_pos_f = pd.DataFrame([{
        'id': 10022,
        'name': "Bot_SHORT",
        'pair': 'BTC/USDC:USDC',
        'direction': 'SHORT',
        'total_invested': 50.0,
        'current_step': 1
    }])

    # Case A: Only 1 physical order exists (actual_ph = 1)
    physical_order_counts = {10022: 1}

    bots_with_partial_orders = []
    
    for _, row in df_pos_f.iterrows():
        bid = int(row['id'])
        bot_inv = float(row['total_invested'] or 0)
        c_step = int(row.get('current_step', 0))
        actual_ph = physical_order_counts.get(bid, 0)

        # The logic we modified in monitor.py:
        if actual_ph < 2 and c_step >= 1 and bot_inv > 0.01:
            gow_ok = True
            try:
                from engine.oneway_netting import gate_oneway_opposite_entry
                gow_ok, _ = gate_oneway_opposite_entry(bid, row['pair'], row['direction'])
            except Exception:
                pass
            if gow_ok:
                bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

    # Assert that the alert for Bot_SHORT is suppressed because it was blocked by the netting gate
    assert len(bots_with_partial_orders) == 0, f"Expected alert to be suppressed, but got: {bots_with_partial_orders}"

    # Case B: If we simulate a bot that is NOT blocked (e.g. no opposite positions exist, so gow_ok is True)
    # Let's close the LONG bot's position to clear the opposite side:
    memory_db.execute("UPDATE trades SET open_qty = 0.0 WHERE bot_id = 10016")
    memory_db.commit()

    # Verify that gate_oneway_opposite_entry is now OK
    ok, reason = oneway_netting.gate_oneway_opposite_entry(10022, 'BTC/USDC:USDC', 'SHORT')
    assert ok, "Expected SHORT bot to be allowed now"

    # Run the UI alert logic again
    bots_with_partial_orders = []
    for _, row in df_pos_f.iterrows():
        bid = int(row['id'])
        bot_inv = float(row['total_invested'] or 0)
        c_step = int(row.get('current_step', 0))
        actual_ph = physical_order_counts.get(bid, 0)

        if actual_ph < 2 and c_step >= 1 and bot_inv > 0.01:
            gow_ok = True
            try:
                from engine.oneway_netting import gate_oneway_opposite_entry
                gow_ok, _ = gate_oneway_opposite_entry(bid, row['pair'], row['direction'])
            except Exception:
                pass
            if gow_ok:
                bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

    # Assert that the alert is NOT suppressed now and shows up in the list
    assert len(bots_with_partial_orders) == 1
    assert bots_with_partial_orders[0] == "Bot_SHORT (1/2)"
