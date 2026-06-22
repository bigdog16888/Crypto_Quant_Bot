"""
engine/migrations/migration_003_fill_claims.py — fill_claims idempotency table (INV-20)

PURPOSE
-------
Implements ARCHITECTURE INVARIANT INV-20: credit_fill() is a singleton per
(bot_id, order_id). The fill_claims table provides an atomic INSERT OR IGNORE
guard at the DB layer, preventing the TOCTOU race where two concurrent callers
(WS event + REST stale-sync, or reconciler + WS) both observe 0 filled_amount
and both proceed to credit the same fill.

HOW IT WORKS
------------
credit_fill() opens a connection, then executes:

    INSERT OR IGNORE INTO fill_claims (bot_id, order_id, caller, claimed_at)
    VALUES (?, ?, ?, ?)

Because order_id has a UNIQUE constraint, only ONE caller wins the insert.
The loser gets SQLITE_CONSTRAINT (silently ignored by OR IGNORE), sees 0
rows affected, and early-returns False without touching filled_amount.

The winner continues to the normal credit path.

IDEMPOTENT
----------
Running this migration multiple times is safe — CREATE TABLE IF NOT EXISTS
and CREATE UNIQUE INDEX IF NOT EXISTS are both idempotent.

TABLE DESIGN
------------
- PRIMARY KEY: (bot_id, order_id) — one row per exchange order per bot.
  We use order_id (exchange-assigned ID) as the canonical dedup key because:
    a) The exchange guarantees it is globally unique for a given account.
    b) client_order_id may be regenerated on GTX chase-retry (new CID, same fill).
- caller: which code path claimed this fill (audit trail only).
- claimed_at: unix timestamp of the first successful claim.

RETENTION
---------
Rows are NOT deleted on TP reset — historical fills must remain claimable
to guard against delayed REST confirmations arriving after a reset.
The reconciler's cycle-boundary guard (cycle_start_time) ensures fills from
prior cycles are not re-credited to the new cycle even if a claim row exists.
"""

import sqlite3
import logging

logger = logging.getLogger("Migration003")

_MIGRATION_ID = "migration_003_fill_claims"


def run(db_path: str) -> None:
    """
    Apply migration_003 to the given SQLite database file.

    This function is idempotent — safe to call on every startup.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()

        # ── Create fill_claims table ───────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fill_claims (
                bot_id     INTEGER NOT NULL,
                order_id   TEXT    NOT NULL,
                caller     TEXT    NOT NULL DEFAULT '',
                claimed_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bot_id, order_id)
            )
        """)

        # ── Unique index on (bot_id, order_id) ────────────────────────────────
        # Redundant with PRIMARY KEY on SQLite, but explicit for clarity and
        # to match the INSERT OR IGNORE semantics expected by credit_fill().
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fill_claims_key
            ON fill_claims (bot_id, order_id)
        """)

        conn.commit()
        logger.info(
            "[MIGRATION-003] ✅ fill_claims table ready "
            "(INV-20: credit_fill singleton guard)."
        )

    except Exception as e:
        logger.error(f"[MIGRATION-003] Failed: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
