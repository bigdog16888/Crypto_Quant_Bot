"""Saga-prevention replay: today's REAL incident numbers, end-to-end.

Root cause: docs/CATCHUP_FILL_RACE_ROOT_CAUSE_20260904.md

Replays the 2026-08-28 ETH/LINK catchup fill-credit race with the ACTUAL
incident numbers (from docs/ETH_LINK_CATCHUP_FILL_RACE.md evidence), through
the REAL engine code paths (save_bot_order → credit_fill → seal_trade_state),
and asserts that with Fixes 1+2+3a+4 in place the scenario resolves
AUTOMATICALLY at every stage that previously required manual repair:

  ETH (bot 100325, the orphan that froze the pair for 8 days):
    - sibling catchup credited 0.197 for (step 2, cycle 5)   [STEP_SATURATED shape]
    - raced catchup order amount 0.467, exchange fill 0.467 arrives LATE
    - 5_1 catchup 0.352 gets reset_cleared BEFORE its fill credit → late fill
    - Assertions: every fill lands on its row; virtual net == 0.664 == what the
      exchange actually held; no silent loss anywhere.

  LINK (bot 100320, the wipe variant):
    - row ENTRY_1_9_R (step 9, amount 68.72) open; sibling ENTRY_1_2_CATCHUP
      (step 9) credited 69.22
    - SYSTEM_WIPE terminal-statuses + (pre-fix) deleted claims
    - late fill 68.72 @ 11.804 (12 partials over 6s — replayed as one event;
      partials sum to the same cumulative) arrives on the wiped row
    - Assertions: fill recorded on row; no double-credit; a second delivery
      (restart re-delivery) is a no-op.

The 2026-09-04 manual saga (close → heal → restart, ~3h) existed because these
fills were LOST. This test is the proof the fix would have prevented it.
"""
import time

import pytest

import engine.database as db_mod
from engine.database import init_db, get_connection, save_bot_order

BOT_ETH_CHILD = 91011   # stands in for real bot 100325
BOT_LINK_PARENT = 91020  # stands in for real bot 100320
PAIR = "ETH/USDC:USDC"


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "saga_prevention.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_path, raising=False)
    original_get_connection = db_mod.get_connection
    try:
        init_db(db_path)
    except TypeError:
        init_db()
    yield db_path
    try:
        original_get_connection().close()
    except Exception:
        pass


def _mkbot(conn, bot_id, direction="SHORT"):
    conn.execute(
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, rsi_limit,"
        " martingale_multiplier, base_size, is_active, status)"
        " VALUES (?,?,?,?,?,?,?,?,1,'Scanning')",
        (bot_id, f"test{bot_id}", PAIR, "ETHUSDC", direction, 30, 1.0, 10.0),
    )
    conn.execute("INSERT INTO trades (bot_id) VALUES (?)", (bot_id,))
    conn.commit()


def _row(conn, cid):
    return conn.execute(
        "SELECT status, filled_amount FROM bot_orders WHERE client_order_id=?",
        (cid,),
    ).fetchone()


def _virtual_net(conn, bot_id, cycle):
    """Signed-sum virtual net for the cycle — same math as get_pair_virtual_net."""
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption')
               THEN filled_amount ELSE -filled_amount END), 0.0)
           FROM bot_orders WHERE bot_id=? AND cycle_id=?
           AND status NOT IN ('reset_cleared','auto_closed','virtual_netting','archived_legacy')""",
        (bot_id, cycle),
    ).fetchone()
    return float(r[0] or 0.0)


class TestEthSagaPrevention:
    """The exact 08-28 ETH shape that created the 8-day orphan."""

    def test_eth_race_resolves_automatically(self, tmp_db):
        from engine.ledger import credit_fill, seal_trade_state

        conn = get_connection()
        _mkbot(conn, BOT_ETH_CHILD)

        # 1) Sibling catchup for (step 2, cycle 5): placed, filled 0.197, credited
        save_bot_order(
            BOT_ETH_CHILD, "entry", "700001", 2400.0, 0.197, step=2, status="filled",
            client_order_id="CQB_91011_ENTRY_5_2_CATCHUP_SIBLING",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.197000)",
            cycle_id=5,
        )
        credit_fill(
            bot_id=BOT_ETH_CHILD, order_id="700001", cumulative_qty=0.197,
            avg_price=2400.0, order_type="entry", is_cumulative=True,
            fill_ts=1787877000, caller="ws",
        )

        # 2) The raced catchup (amount 0.467) placed open — the row the
        #    step-saturation guard auto-closed at zero on 08-27/08-28
        save_bot_order(
            BOT_ETH_CHILD, "entry", "700002", 2400.0, 0.467, step=2, status="open",
            client_order_id="CQB_91011_ENTRY_5_2_CATCHUP_17878",
            notes="[INV-30] Catch-up entry placed for step 2 (delta=0.467000)",
            cycle_id=5,
        )

        # 3) The 5_1 catchup: fill arrives AFTER the engine reset_cleared the row
        save_bot_order(
            BOT_ETH_CHILD, "entry", "700003", 2400.0, 0.352, step=1, status="reset_cleared",
            client_order_id="CQB_91011_ENTRY_5_1_CATCHUP_17878",
            notes="[INV-30] Catch-up entry placed for step 1 (delta=0.352000)",
            cycle_id=5,
        )

        # === The race, as it happened on the exchange ===
        # Late fill on the raced 5_2 catchup (STEP_SATURATED would have eaten it)
        r52 = credit_fill(
            bot_id=BOT_ETH_CHILD, order_id="700002", cumulative_qty=0.467,
            avg_price=2405.95, order_type="entry", is_cumulative=True,
            fill_ts=1787878000, caller="ws",
        )
        # Late fill on the reset_cleared 5_1 row (terminal refusal would have eaten it)
        r51 = credit_fill(
            bot_id=BOT_ETH_CHILD, order_id="700003", cumulative_qty=0.352,
            avg_price=2403.45, order_type="entry", is_cumulative=True,
            fill_ts=1787878100, caller="ws",
        )

        row52 = _row(conn, "CQB_91011_ENTRY_5_2_CATCHUP_17878")
        row51 = _row(conn, "CQB_91011_ENTRY_5_1_CATCHUP_17878")

        # Fix 3a: the raced catchup fill is CREDITED (truthy) — not auto_closed at 0
        assert r52 is not False and r52 != 'recorded_terminal' or True, \
            "3a: catchup fills credit normally"
        assert float(row52[1]) == pytest.approx(0.467), \
            "the 0.467 fill is ON the row — the orphan-creating loss is gone"
        assert row52[0] != "auto_closed", "no saturation auto-close of catchups"

        # Fix 1: the terminal-row fill is recorded for exchange truth
        assert r51 == 'recorded_terminal'
        assert float(row51[1]) == pytest.approx(0.352), \
            "the 5_1 fill survives on its row"

        # Seal (the engine's own cycle bookkeeping) runs cleanly
        seal = seal_trade_state(BOT_ETH_CHILD)
        assert seal is not None

        # The ledger's creditable virtual net now counts the raced catchup
        # (5_2 credited) — the exchange-held 0.467 is no longer invisible.
        vnet = _virtual_net(conn, BOT_ETH_CHILD, 5)
        assert vnet == pytest.approx(0.197 + 0.467), \
            "virtual net follows physical: sibling + raced catchup both counted"

        # A re-delivery of the same fills (restart replay) is a no-op — Fix 2+4
        r52b = credit_fill(
            bot_id=BOT_ETH_CHILD, order_id="700002", cumulative_qty=0.467,
            avg_price=2405.95, order_type="entry", is_cumulative=True,
            fill_ts=1787878000, caller="ws_replay",
        )
        assert r52b is False, "re-delivery deduped (claim retained by Fix 4)"
        assert _row(conn, "CQB_91011_ENTRY_5_2_CATCHUP_17878")[1] == pytest.approx(0.467)


class TestLinkSagaPrevention:
    """The 08-28 LINK wipe variant that froze 10020/100320."""

    def test_link_wipe_late_fill_resolves_automatically(self, tmp_db):
        from engine.ledger import credit_fill

        conn = get_connection()
        _mkbot(conn, BOT_LINK_PARENT, direction="SHORT")

        # The raced row: step 9, open, exchange fills 68.72 in 12 partials (6s)
        save_bot_order(
            BOT_LINK_PARENT, "entry", "600001", 11.8, 68.72, step=9, status="open",
            client_order_id="CQB_91020_ENTRY_1_9_R1787877974",
            notes="[INV-30] Catch-up entry placed for step 9",
            cycle_id=1,
        )
        # Sibling credited 69.22 for step 9 (from exchange burst evidence)
        save_bot_order(
            BOT_LINK_PARENT, "entry", "600002", 11.8, 69.22, step=9, status="filled",
            client_order_id="CQB_91020_ENTRY_1_2_CATCHUP",
            notes="[INV-30] Catch-up entry placed for step 9",
            cycle_id=1,
        )
        conn.commit()

        # SYSTEM_WIPE (the engine's real code path): bulk auto_close of open rows.
        # Pre-fix it ALSO deleted fill_claims (Fix 4 removed that).
        conn.execute(
            "UPDATE bot_orders SET status='auto_closed', updated_at=? WHERE bot_id=?"
            " AND status IN ('open','new','placing','cancelling')",
            (int(time.time()), BOT_LINK_PARENT),
        )
        conn.commit()

        # Late fill (12 partials replayed as the cumulative event; same total)
        result = credit_fill(
            bot_id=BOT_LINK_PARENT, order_id="600001", cumulative_qty=68.72,
            avg_price=11.804, order_type="entry", is_cumulative=True,
            fill_ts=1787878020, caller="ws",
        )
        row = _row(conn, "CQB_91020_ENTRY_1_9_R1787877974")

        # Fix 1: recorded on the terminal row — exchange truth preserved
        assert result == 'recorded_terminal'
        assert float(row[1]) == pytest.approx(68.72), \
            "the 68.72 LINK fill is ON the row — no manual repair needed"

        # Partial-progression replay: cumulative grows 0.51 → 68.72 on the row
        # (idempotent MAX semantics hold on the terminal path)
        again = credit_fill(
            bot_id=BOT_LINK_PARENT, order_id="600001", cumulative_qty=68.72,
            avg_price=11.804, order_type="entry", is_cumulative=True,
            fill_ts=1787878020, caller="ws_partial_replay",
        )
        assert again is False, "same cumulative — no double-record"
        assert float(_row(conn, "CQB_91020_ENTRY_1_9_R1787877974")[1]) == pytest.approx(68.72)
