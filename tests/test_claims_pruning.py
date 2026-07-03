import sqlite3
import time
import uuid
from unittest.mock import patch, MagicMock
import pytest

import engine.database as database
from engine.database import (
    _prune_cross_reduction_claims,
    _prune_fill_claims,
    init_db,
    get_connection,
)

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_prune_{db_id}?mode=memory&cache=shared'
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


def test_prune_cross_reduction_claims_removes_old_rows(memory_db):
    # Insert 5 rows with claimed_at < 30 days ago (e.g. 31 days ago)
    old_time = int(time.time()) - (31 * 86400)
    for i in range(5):
        memory_db.execute("""
            INSERT INTO cross_reduction_claims (source_order_id, source_bot_id, target_bot_id, reduction_qty, claimed_at)
            VALUES (?, 10001, 10002, 0.1, ?)
        """, (f"old_src_order_{i}", old_time))
    memory_db.commit()

    # Verify rows exist
    count = memory_db.execute("SELECT COUNT(*) FROM cross_reduction_claims").fetchone()[0]
    assert count == 5

    # Run pruning
    _prune_cross_reduction_claims(memory_db, retention_days=30)

    # Assert 0 rows remain
    count = memory_db.execute("SELECT COUNT(*) FROM cross_reduction_claims").fetchone()[0]
    assert count == 0


def test_prune_cross_reduction_claims_keeps_recent_rows(memory_db):
    # Insert 5 rows with claimed_at = now
    now_time = int(time.time())
    for i in range(5):
        memory_db.execute("""
            INSERT INTO cross_reduction_claims (source_order_id, source_bot_id, target_bot_id, reduction_qty, claimed_at)
            VALUES (?, 10001, 10002, 0.1, ?)
        """, (f"now_src_order_{i}", now_time))
    memory_db.commit()

    # Run pruning
    _prune_cross_reduction_claims(memory_db, retention_days=30)

    # Assert 5 rows remain
    count = memory_db.execute("SELECT COUNT(*) FROM cross_reduction_claims").fetchone()[0]
    assert count == 5


def test_prune_fill_claims_removes_old_rows(memory_db):
    # Insert 5 rows with claimed_at < 30 days ago (e.g. 31 days ago)
    old_time = int(time.time()) - (31 * 86400)
    for i in range(5):
        memory_db.execute("""
            INSERT INTO fill_claims (bot_id, order_id, caller, claimed_at)
            VALUES (?, ?, 'caller', ?)
        """, (10001 + i, f"old_order_{i}", old_time))
    memory_db.commit()

    # Verify rows exist
    count = memory_db.execute("SELECT COUNT(*) FROM fill_claims").fetchone()[0]
    assert count == 5

    # Run pruning
    _prune_fill_claims(memory_db, retention_days=30)

    # Assert 0 rows remain
    count = memory_db.execute("SELECT COUNT(*) FROM fill_claims").fetchone()[0]
    assert count == 0


def test_prune_fill_claims_keeps_recent_rows(memory_db):
    # Insert 5 rows with claimed_at = now
    now_time = int(time.time())
    for i in range(5):
        memory_db.execute("""
            INSERT INTO fill_claims (bot_id, order_id, caller, claimed_at)
            VALUES (?, ?, 'caller', ?)
        """, (10001 + i, f"now_order_{i}", now_time))
    memory_db.commit()

    # Run pruning
    _prune_fill_claims(memory_db, retention_days=30)

    # Assert 5 rows remain
    count = memory_db.execute("SELECT COUNT(*) FROM fill_claims").fetchone()[0]
    assert count == 5


@patch('engine.database._prune_cross_reduction_claims')
@patch('engine.database._prune_fill_claims')
def test_prune_runs_on_startup(mock_prune_fill, mock_prune_cross, memory_db):
    # Call init_db()
    init_db()

    # Assert both pruning functions called
    assert mock_prune_cross.called
    assert mock_prune_fill.called
