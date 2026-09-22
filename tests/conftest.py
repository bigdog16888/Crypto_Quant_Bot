import os
import sys
import sqlite3
from pathlib import Path

# CRITICAL: Set PYTEST_RUNNING BEFORE any engine imports
# This must be the very first thing in conftest.py
os.environ["PYTEST_RUNNING"] = "1"
os.environ["TESTING_MODE"] = "True"

# Hard guard: block any connection to the live crypto_bot.db
_LIVE_DB = Path(r"D:\Crypto_Quant_Bot\crypto_bot.db").resolve()
_ORIG_CONNECT = sqlite3.connect

def _GUARDED_CONNECT(path, *args, **kwargs):
    is_live = False
    try:
        if isinstance(path, (str, os.PathLike)):
            p = Path(str(path).replace('file:', '').split('?')[0]).resolve()
            is_live = (p == _LIVE_DB)
    except Exception:
        is_live = False

    if is_live and 'mode=ro' not in str(path).lower():
        raise RuntimeError(f"LIVE DB WRITE BLOCKED by test isolation guard: {path}")

    return _ORIG_CONNECT(path, *args, **kwargs)

sqlite3.connect = _GUARDED_CONNECT

import engine.write_queue as wq_module
wq_module.WriteQueue._bypass = True
wq_module.WriteQueue._instance = None

# ---------------------------------------------------------------------------
# Cross-test DB connection isolation (Step 6, 2026-09-15).
# ...
import threading
import engine.database as _ed_lazy

import pytest


@pytest.fixture(autouse=True)
def reset_forensic_config(monkeypatch):
    """Reset ALLOW_FORENSIC_ADOPT to False between every test to prevent config bleed."""
    from config.settings import config
    monkeypatch.setattr(config, "ALLOW_FORENSIC_ADOPT", False)


@pytest.fixture(autouse=True, scope="function")
def _isolate_db_connections():
    # Lazy import AFTER env vars are set and guard is installed
    import engine.database as _ed
    saved_path = _ed.DB_PATH

    _force_close_cached_conn()

    yield

    _force_close_cached_conn()
    # Restore only if not overridden by temp_db fixture
    if _ed.DB_PATH == saved_path:
        _ed.DB_PATH = saved_path


def _force_close_cached_conn():
    try:
        # Safely close any active connection before resetting
        conn = getattr(_ed_lazy._local, 'connection', getattr(_ed_lazy._local, 'conn', None))
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        _ed_lazy._local = threading.local()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session-scoped temp DB redirect — autouse for ALL tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _temp_db_redirect(tmp_path_factory):
    """Redirect engine.database.DB_PATH to a temp file for the entire test session."""
    import engine.database as _ed
    temp_dir = tmp_path_factory.mktemp("db")
    temp_db_path = temp_dir / "test_session.db"
    _ed.DB_PATH = str(temp_db_path)
    # Force re-init on the temp DB
    _force_close_cached_conn()
    _ed.init_db()
    yield
    # Cleanup handled by tmp_path_factory


# ---------------------------------------------------------------------------
# Function-scoped temp DB fixture for tests needing explicit connection
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(monkeypatch, tmp_path, request):
    """
    Creates a temporary SQLite database with full schema initialized.
    Returns the sqlite3.Connection to the temp DB.

    Tests that need isolation from production DB should use this fixture
    (add `temp_db` parameter). Tests marked @pytest.mark.no_db skip this.
    """
    # Allow opt-out for pure mock/unit tests
    if request.node.get_closest_marker("no_db"):
        yield None
        return

    db_file = tmp_path / f"test_{request.node.name}.db"
    db_path = str(db_file)

    # Patch the module-level DB_PATH to the temp file
    import engine.database as _ed
    monkeypatch.setattr(_ed, "DB_PATH", db_path, raising=False)

    _force_close_cached_conn()

    # Initialize schema on the temp DB
    try:
        _ed.init_db(db_path)
    except TypeError:
        _ed.init_db()

    conn = _ed.get_connection()

    try:
        yield conn
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        _force_close_cached_conn()


@pytest.fixture
def temp_db_path(temp_db):
    """Return the temp DB path string for tests that need it directly."""
    if temp_db is None:
        return None
    cursor = temp_db.execute("PRAGMA database_list")
    for row in cursor.fetchall():
        if row[1] == "main":
            return row[2]
    return None