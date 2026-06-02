import sys
import os
import sqlite3
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

from engine.reconciler import (
    StateReconciler, ReconciliationAction, ReconciliationResult,
    BotState, ExchangePosition
)

def _make_in_memory_db(bot_qtys):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            order_type TEXT,
            filled_amount REAL,
            status TEXT,
            cycle_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, side TEXT, size REAL
        )
    """)
    for bot_id, qty in bot_qtys:
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, filled_amount, status, cycle_id) "
            "VALUES (?, 'entry', ?, 'filled', 1)",
            (bot_id, qty)
        )
    conn.commit()
    return conn

def _make_bot(bot_id, name, pair, direction, qty, avg_entry, confirmed=True):
    total_invested = qty * avg_entry
    return BotState(
        bot_id=bot_id, name=name, pair=pair, direction=direction,
        is_active=True, in_trade=total_invested > 0, total_invested=total_invested,
        avg_entry_price=avg_entry, target_tp_price=avg_entry * 1.01,
        current_step=1, basket_start_time=1000000,
        entry_order_id=None, tp_order_id=None, has_confirmed_entry=confirmed
    )

BOT_QTY = 0.016
AVG_PRICE = 67200.0

db = _make_in_memory_db([(10004, BOT_QTY)])

@patch('engine.reconciler.safe_wipe_bot', return_value=True)
def run_debug(mock_wipe):
    reconciler = StateReconciler(exchanges={})
    bots = [_make_bot(10004, "Ghost_Long", "BTC/USDC", "LONG", BOT_QTY, AVG_PRICE)]
    positions = []  # Exchange is flat

    with patch('engine.reconciler.get_connection', return_value=db):
        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": positions})
        print("RESULTS:", [(r.bot_id, r.action_taken, r.details) for r in results])
        print("safe_wipe_bot called:", mock_wipe.called)

run_debug()
