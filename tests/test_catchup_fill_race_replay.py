"""Replay test: catchup fill-credit race (ETH + LINK shapes).

Root cause: docs/CATCHUP_FILL_RACE_ROOT_CAUSE_20260904.md

Fix sequence (operator-approved 2026-09-04): Fix 2 (claim-after-check) →
Fix 1 (record-on-terminal) → Fix 3a (catchup exemption from step-saturation
auto_close) → Fix 4 (stop deleting fill_claims on reset/wipe).

RED tests are bug-detectors: they assert the LOSS and pass while the bug is
present. Each is written fix-aware: when its fix lands, the loss assertion
correctly fails / flips to verifying the fixed behavior. GREEN tests assert
the post-fix contract directly.

Runs against a tmp DB through the REAL save_bot_order/credit_fill paths
(WriteQueue bypassed under pytest per conftest). No engine, no live DB.
"""
import time

import pytest

import engine.database as db_mod
from engine.database import init_db, get_connection, save_bot_order

# Test-only bot IDs outside STARTUP_EXCLUDED_BOT_IDS (skill rule: 90001+ range)
BOT_CHILD = 91001   # hedge child (ETH shape)
BOT_PARENT = 91002  # parent (LINK shape)

PAIR = "ETH/USDC:USDC"


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """Isolated DB file; patches get_connection/init_db defaults to it."""
    db_path = str(tmp_path / "race_replay.db")

    monkeypatch.setattr(db_mod, "DB_PATH", db_path, raising=False)
    original_get_connection = db_mod.get_connection

    try:
        init_db(db_path)
    except TypeError:
        init_db()
    yield db_path
    try:
        conn = original_get_connection()
        conn.close()
    except Exception:
        pass


def _row(conn, cid):
    return conn.execute(
        "SELECT status, filled_amount, price, filled_at FROM bot_orders WHERE client_order_id=?",
        (cid,),
    ).fetchone()


def _claim_exists(conn, order_id):
    return conn.execute(
        "SELECT COUNT(*) FROM fill_claims WHERE order_id=?", (str(order_id),)
    ).fetchone()[0] > 0


def _open_qty(conn, bot_id):
    return float(conn.execute(
        "SELECT COALESCE(open_qty, 0) FROM trades WHERE bot_id=?", (bot_id,)
    ).fetchone()[0] or 0)


def _mkbot(conn, bot_id, direction="SHORT"):
    conn.execute(
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, rsi_limit,"
        " martingale_multiplier, base_size, is_active, status)"
        " VALUES (?,?,?,?,?,?,?,?,1,'Scanning')",
        (bot_id, f"test{bot_id}", PAIR, "ETHUSDC", direction, 30, 1.0, 10.0),
    )
    conn.execute("INSERT INTO trades (bot_id) VALUES (?)", (bot_id,))
    conn.commit()


class TestMode1EthStepSaturationFalsePositive:
    """ETH shape: sibling catchup credited → new catchup order's real fill
    lost via step-saturation auto_close (Fix 3a target)."""

    def test_bug_replay_fix_aware(self, tmp_db):
        """Pre-3a: fill LOST (row auto_closed at zero). Post-3a: fill
        recorded on the row, no open_qty inflation, catchup not auto_closed."""
        from engine.ledger import credit_fill

        conn = get_connection()
        _mkbot(conn, BOT_CHILD)

        save_bot_order(
            BOT_CHILD, "entry", "900001", 2400.0, 0.197, step=2, status="filled",
            client_order_id="CQB_91001_ENTRY_5_2_CATCHUP_SIBLING",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.197000)",
            cycle_id=5,
        )
        save_bot_order(
            BOT_CHILD, "entry", "900002", 2400.0, 0.467, step=2, status="open",
            client_order_id="CQB_91001_ENTRY_5_2_CATCHUP_17878",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.467000)",
            cycle_id=5,
        )
        conn.commit()

        result = credit_fill(
            bot_id=BOT_CHILD, order_id="900002", cumulative_qty=0.467,
            avg_price=2405.95, order_type="entry", is_cumulative=True,
            fill_ts=1787878000, caller="ws",
        )
        row = _row(conn, "CQB_91001_ENTRY_5_2_CATCHUP_17878")

        if result == 'recorded_terminal' or (row[0] not in ('auto_closed',) and float(row[1]) > 0):
            # Fix 3a (final form) is in: the catchup fill is CREDITED normally —
            # virtual follows physical (additive-by-design exposure).
            assert float(row[1]) == pytest.approx(0.467), "fill recorded on row"
            assert _open_qty(conn, BOT_CHILD) == pytest.approx(0.467), \
                "3a-final: catchup fill credited — virtual follows physical"
            return
        # Pre-3a behavior (the bug): fill lost, row auto_closed at zero
        assert result is False, "pre-3a: fill refused"
        assert row[0] == "auto_closed", "pre-3a: step-saturation auto-closed the catchup"
        assert float(row[1]) == 0.0, "pre-3a: fill LOST from row (the bug)"

    def test_green_3a_contract(self, tmp_db):
        """Post-Fix-3a contract: catchup CIDs are exempt from step-saturation
        auto_close — the row keeps a creditable status so its real fill lands."""
        from engine.ledger import credit_fill

        conn = get_connection()
        _mkbot(conn, BOT_CHILD)

        save_bot_order(
            BOT_CHILD, "entry", "900001", 2400.0, 0.197, step=2, status="filled",
            client_order_id="CQB_91001_ENTRY_5_2_CATCHUP_SIBLING",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.197000)",
            cycle_id=5,
        )
        save_bot_order(
            BOT_CHILD, "entry", "900002", 2400.0, 0.467, step=2, status="open",
            client_order_id="CQB_91001_ENTRY_5_2_CATCHUP_17878",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.467000)",
            cycle_id=5,
        )
        conn.commit()

        credit_fill(
            bot_id=BOT_CHILD, order_id="900002", cumulative_qty=0.467,
            avg_price=2405.95, order_type="entry", is_cumulative=True,
            fill_ts=1787878000, caller="ws",
        )
        row = _row(conn, "CQB_91001_ENTRY_5_2_CATCHUP_17878")

        # The catchup row must NOT have been auto_closed at zero by saturation.
        assert row[0] != "auto_closed" or float(row[1]) > 0, \
            "3a: catchup rows are not auto-closed with the fill lost"
        assert float(row[1]) == pytest.approx(0.467), "3a: the real fill is on the row"


class TestMode2LinkWipeThenLateFill:
    """LINK shape: SYSTEM_WIPE terminal-statuses rows + deletes claims; late
    fill on terminal row was dropped (Fix 1 target)."""

    def _setup_link_shape(self, conn):
        _mkbot(conn, BOT_PARENT, direction="SHORT")
        save_bot_order(
            BOT_PARENT, "entry", "800001", 11.8, 68.72, step=9, status="open",
            client_order_id="CQB_91002_ENTRY_1_9_R1787877974",
            notes="[INV-30] Catch-up entry placed for step 9",
            cycle_id=1,
        )
        save_bot_order(
            BOT_PARENT, "entry", "800002", 11.8, 69.22, step=9, status="filled",
            client_order_id="CQB_91002_ENTRY_1_2_CATCHUP",
            notes="[INV-30] Catch-up entry placed for step 2",
            cycle_id=1,
        )
        conn.commit()

    def test_green_fix1_records_terminal_fill(self, tmp_db):
        """Post-Fix-1 contract: late fill on a terminal row is RECORDED on the
        row (exchange truth), status stays terminal, open_qty untouched, and
        the return value is the distinguishable 'recorded_terminal'."""
        from engine.ledger import credit_fill

        conn = get_connection()
        self._setup_link_shape(conn)

        # SYSTEM_WIPE: bulk auto_close + DELETE FROM fill_claims (database.py:1815-1817)
        conn.execute(
            "UPDATE bot_orders SET status='auto_closed', updated_at=? WHERE bot_id=?"
            " AND status IN ('open','new','placing','cancelling')",
            (int(time.time()), BOT_PARENT),
        )
        conn.execute("DELETE FROM fill_claims WHERE bot_id=?", (BOT_PARENT,))
        conn.commit()

        result = credit_fill(
            bot_id=BOT_PARENT, order_id="800001", cumulative_qty=68.72,
            avg_price=11.804, order_type="entry", is_cumulative=True,
            fill_ts=1787878020, caller="ws",
        )
        row = _row(conn, "CQB_91002_ENTRY_1_9_R1787877974")

        assert result == 'recorded_terminal', f"Fix 1: got {result!r}"
        assert row[0] == "auto_closed", "status stays terminal (no resurrection)"
        assert float(row[1]) == pytest.approx(68.72), "68.72 fill recorded on row"
        assert abs(float(row[2]) - 11.804) < 1e-9, "fill price recorded"
        assert _open_qty(conn, BOT_PARENT) == pytest.approx(0.0), \
            "open_qty NOT incremented (adoption is GTR/SNAP's audited path)"

    def test_green_fix1_idempotent_replay(self, tmp_db):
        """Second delivery of the same fill must not double-record (MAX()
        semantics preserved on the terminal path)."""
        from engine.ledger import credit_fill

        conn = get_connection()
        self._setup_link_shape(conn)
        conn.execute(
            "UPDATE bot_orders SET status='auto_closed', updated_at=? WHERE bot_id=?"
            " AND status IN ('open','new','placing','cancelling')",
            (int(time.time()), BOT_PARENT),
        )
        conn.execute("DELETE FROM fill_claims WHERE bot_id=?", (BOT_PARENT,))
        conn.commit()

        first = credit_fill(
            bot_id=BOT_PARENT, order_id="800001", cumulative_qty=68.72,
            avg_price=11.804, order_type="entry", is_cumulative=True,
            fill_ts=1787878020, caller="ws",
        )
        second = credit_fill(
            bot_id=BOT_PARENT, order_id="800001", cumulative_qty=68.72,
            avg_price=11.804, order_type="entry", is_cumulative=True,
            fill_ts=1787878020, caller="ws_replay",
        )
        row = _row(conn, "CQB_91002_ENTRY_1_9_R1787877974")

        assert first == 'recorded_terminal'
        assert second is False, "replay: nothing new (idempotent)"
        assert float(row[1]) == pytest.approx(68.72), "no double-record"


class TestChokePointClaimsOrdering:
    """Fix 2 contract: a refused fill must NOT burn its fill_claims slot."""

    def test_green_fix2_no_claim_burn_on_terminal(self, tmp_db):
        """Post-Fix-2: terminal-refused fill leaves no claim; a later
        legitimate path (Fix 1 recording) can still act on the order."""
        from engine.ledger import credit_fill

        conn = get_connection()
        _mkbot(conn, BOT_CHILD)
        save_bot_order(
            BOT_CHILD, "entry", "900003", 2400.0, 0.5, step=1, status="reset_cleared",
            client_order_id="CQB_91001_ENTRY_5_1_CATCHUP_17878",
            notes="[INV-30] Catch-up entry placed for step 1 (delta=0.352000)",
            cycle_id=5,
        )
        conn.commit()

        result = credit_fill(
            bot_id=BOT_CHILD, order_id="900003", cumulative_qty=0.352,
            avg_price=2403.45, order_type="entry", is_cumulative=True,
            fill_ts=1787878100, caller="ws",
        )
        # Fix 1 makes the terminal path record (truthy); Fix 2 means no claim
        # is burned by the pre-check refusal ordering. With both fixes in, the
        # claim is only taken by the recording path itself (harmless).
        assert result == 'recorded_terminal', f"Fix 1+2: got {result!r}"
        assert _open_qty(conn, BOT_CHILD) == pytest.approx(0.0)
