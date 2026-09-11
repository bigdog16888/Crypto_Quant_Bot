"""Operability primitives (operability session, 2026-09-11).

  - Dead-man heartbeat: write_heartbeat() per engine cycle; check_heartbeat()
    flags staleness. ALERT-ONLY by design — nothing in the engine or these
    helpers restarts anything (restarts are operator-gated from a named,
    fully-tested hash).
  - DB backup via the sqlite online-backup API (safe on a live DB), called
    on graceful shutdown; restore is force-guarded so an operator can never
    silently overwrite an existing live DB with a backup.
"""
import os
import time
import shutil
import sqlite3
import logging

from config.settings import config
from engine import database
from engine.database import get_connection

logger = logging.getLogger("engine.ops")

HEARTBEAT_FILENAME = "engine.heartbeat"


def _heartbeat_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)),
                        HEARTBEAT_FILENAME)


def write_heartbeat() -> None:
    """One beat per engine cycle. Never raises — a failed beat is the
    dead-man signal itself, not a crash source."""
    try:
        with open(_heartbeat_path(), "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def heartbeat_age_seconds():
    """Seconds since the last beat, or None when unreadable/missing."""
    try:
        with open(_heartbeat_path()) as f:
            ts = int(f.read().strip())
        return max(0, time.time() - ts)
    except Exception:
        return None


def check_heartbeat(stale_seconds: int = 300) -> bool:
    """True = STALE (missing or older than stale_seconds). Alert-only."""
    age = heartbeat_age_seconds()
    return age is None or age > stale_seconds


def backup_database(dest_dir=None, keep: int = 20):
    """Online backup of the live crypto_bot.db (sqlite backup API).

    Returns the backup path. Safe while the engine is running — the
    backup API copies pages under the source connection's own locking.
    Keeps the newest `keep` backups, prunes older ones.
    """
    dest_dir = dest_dir or os.path.join(config.ROOT_DIR, "backups")
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(
        dest_dir, f"crypto_bot_backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(
            dest_dir,
            f"crypto_bot_backup_{time.strftime('%Y%m%d_%H%M%S')}_{n}.db")
    src = get_connection()
    dest = sqlite3.connect(path)
    src.backup(dest)
    dest.close()
    _prune_backups(dest_dir, keep)
    return path


def _prune_backups(dest_dir: str, keep: int) -> None:
    try:
        files = sorted(
            (f for f in os.listdir(dest_dir) if f.startswith("crypto_bot_backup_")),
            key=lambda f: os.path.getmtime(os.path.join(dest_dir, f)),
            reverse=True,
        )
        for old in files[keep:]:
            os.remove(os.path.join(dest_dir, old))
    except Exception:
        pass


def restore_database(path: str, force: bool = False) -> str:
    """Restore a backup over the live DB. REFUSES to overwrite an existing
    live DB unless force=True — an operator must stop the engine and
    explicitly confirm. Raises RuntimeError on refusal or bad backup."""
    if not os.path.exists(path):
        raise RuntimeError(f"backup not found: {path}")
    probe = sqlite3.connect(path)
    ok = probe.execute("PRAGMA integrity_check").fetchone()[0]
    probe.close()
    if ok != "ok":
        raise RuntimeError(f"backup failed integrity check: {path}")
    live = os.path.abspath(database.DB_PATH)
    if os.path.exists(live) and not force:
        raise RuntimeError(
            "restore_database refuses to overwrite an existing live DB — "
            "stop the engine and pass force=True after verifying."
        )
    shutil.copy2(path, live)
    return live
