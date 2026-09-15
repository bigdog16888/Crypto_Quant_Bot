import os
import sys

# Set TESTING_MODE environment variable so that config.settings initializes it correctly for all unit tests.
os.environ["TESTING_MODE"] = "True"

# Set PYTEST_RUNNING so that engine/database.py skips backup_database(),
# closes init connections, and skips heal_zombie_bots/auto_create_hedge
# during test runs (these are startup-only operations that lock temp DBs).
os.environ["PYTEST_RUNNING"] = "1"

# Ensure WriteQueue bypass is active before any engine import.
# Under pytest, WriteQueue.__init__ already sets _bypass=True because
# 'pytest' is in sys.modules naturally. We additionally force it at class level
# and drop any pre-existing singleton so no worker thread ever starts during tests.
import engine.write_queue as wq_module
wq_module.WriteQueue._bypass = True
wq_module.WriteQueue._instance = None

# ---------------------------------------------------------------------------
# Cross-test DB connection isolation (Step 6, 2026-09-15).
#
# engine.database.get_connection() caches a SQLite connection in the module-global
# `database._local` thread-local keyed on `database.DB_PATH`, and opens it in WAL
# mode. Several tests point DB_PATH at a temp dir then `rmtree` it in teardown
# WITHOUT closing/resetting `_local`. On Windows the still-open connection keeps
# the `-wal`/`-shm` files locked (PermissionError WinError 32), and the NEXT test
# that reuses the dead cached connection hits "unable to open database file" /
# "NoneType has no attribute cursor". This cascaded and broke tests that pass in
# isolation (test_ghost_clearing x2, test_snap_allocate_gate, test_streamlit_smoke).
#
# Fix: an autouse fixture that, around EVERY test, snapshots + restores
# `database.DB_PATH` and force-closes/clears the cached `_local` connection so no
# dead or locked handle ever leaks across tests. Non-invasive: test-only, no engine
# code changed.
# ---------------------------------------------------------------------------
import threading
import engine.database as _ed

import pytest


@pytest.fixture(autouse=True, scope="function")
def _isolate_db_connections():
    # Save prior global state (in case an earlier test left it mutated).
    saved_path = _ed.DB_PATH

    # Clear any cached connection from a previous test before this one starts.
    _force_close_cached_conn()

    yield

    # After the test: force-close the cached connection and reset the thread-local
    # so the next test cannot inherit a dead/locked handle.
    _force_close_cached_conn()
    _ed.DB_PATH = saved_path


def _force_close_cached_conn():
    local = getattr(_ed, "_local", None)
    if local is None:
        return
    conn = getattr(local, "connection", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    # Wipe the cached connection + its path marker unconditionally.
    try:
        local.connection = None
        local.connection_db_path = None
    except Exception:
        pass
    # Defensive: replace the thread-local entirely so no stale attribute lingers.
    try:
        _ed._local = threading.local()
    except Exception:
        pass
