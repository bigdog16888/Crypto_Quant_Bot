"""Real-path tests for the downtime-TP cycle-advance branch (fix/downtime-cycle-advance).

Complements tests/test_downtime_tp_cycle_advance.py by driving the ACTUAL engine
code paths (not inline re-implementations):

  1. test_precommit_resolves_downtime_tp_via_real_code — drives the real
     StateReconciler._reconstruct_offline_fills_internal with a fake exchange
     whose fetch_order returns the exact 2026-09-09 10016 TP fill shape
     (#1200442005, filled 0.231 @ 78730.9, status 'filled'). Asserts the real
     PRE-COMMIT-RESOLVE branch: row credited + status promoted + cascade
     registered (get_pending_tp_count == 1) — the gap never forms.

  2. test_end_to_end_drain_advances_cycle — continues from (1): the registered
     cascade is drained through the REAL handle_tp_completion →
     reset_bot_after_tp. Asserts trades.cycle_id advanced 19 → 20 and
     close_type recorded — the exact two manual heals now automated.

  3. test_execute_entry_real_guard_self_heals — drives the REAL
     BotExecutor.execute_entry DEDUP-GUARD with a mocked exchange, a flat
     closed-cycle trades row, and the colliding filled ENTRY_19_1 row. Asserts
     the guard self-advances the cycle and the entry PLACES under cycle 20
     (clientOrderId CQB_92016_ENTRY_20_1 on the fake exchange).

Standard: saga-prevention replay — exact incident numbers end-to-end through
real code; the wedge never survives to a second blocked entry attempt.
"""
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

import engine.database as db_mod
from engine.database import init_db, get_connection, save_bot_order

BOT = 92016
PAIR = "BTC/USDC:USDC"
OLD_CYCLE = 19
NEW_CYCLE = 20
TP_ORDER_ID = "1200442005"
TP_CID = f"CQB_{BOT}_TP_19_8_R1788855730"
TP_PRICE = 78730.9
TP_QTY = 0.231
TP_FILL_TS_S = 1788855730  # the _R suffix of the real TP CID, seconds


class _FakeExchange:
    """Minimal exchange seam: only what _reconstruct_offline_fills_internal,
    handle_tp_completion and execute_entry's entry path touch, with the REAL
    incident shapes."""
    def __init__(self, tp_order=None, position_qty=0.0):
        self._tp = tp_order
        self._pos_qty = position_qty
        self.placed = []

    # --- seam for reconciler PRE-COMMIT-RESOLVE + reset verification ---
    def fetch_order(self, cid, pair):
        if self._tp is not None and str(cid) == TP_CID:
            return self._tp
        raise KeyError("unknown cid")

    def fetch_positions(self):
        return [{
            "symbol": PAIR, "contracts": self._pos_qty, "qty": abs(self._pos_qty),
            "net_qty": self._pos_qty, "side": "long" if self._pos_qty >= 0 else "short",
            "unrealizedPnl": 0.0, "entryPrice": 0.0,
        }]

    def fetch_open_orders(self, pair=None):
        return []

    def fetch_closed_orders(self, symbol=None, limit=50):
        return []

    # --- seam for execute_entry ---
    def get_best_bid_ask(self, pair):
        return 78598.0, 78602.0

    def validate_order(self, pair, side, amount, price):
        return True, amount, price, None

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        oid = str(710000000 + len(self.placed))
        self.placed.append({
            "id": oid, "symbol": symbol, "type": type_, "side": side,
            "amount": amount, "price": price, "status": "new",
            "clientOrderId": (params or {}).get("newClientOrderId", ""),
        })
        return self.placed[-1]


def _tp_fill_order():
    # Exact exchange truth of the 2026-09-09 incident (demo FAPI shape):
    # ccxt-normalized dict as the reconciler's exch_order path expects.
    return {
        "id": TP_ORDER_ID, "status": "filled", "filled": TP_QTY,
        "amount": TP_QTY, "average": TP_PRICE, "price": TP_PRICE,
        "clientOrderId": TP_CID, "timestamp": TP_FILL_TS_S * 1000,
        "lastTradeTimestamp": TP_FILL_TS_S * 1000,
    }


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """Temp DB with FULL singleton reset (repo pattern from test_entry_dedup):
    the thread-local connection must be dropped between tests or later tests
    silently read the previous test's DB."""
    import engine.database as database
    db_path = str(tmp_path / "downtime_realpath.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_path, raising=False)
    if hasattr(database._local, 'connection') and database._local.connection:
        database._local.connection.close()
        database._local.connection = None
    try:
        init_db(db_path)
    except TypeError:
        init_db()
    # The TP-cascade registry is module-level state — drain it so each test
    # starts clean (set-semantics would otherwise leak between tests).
    from engine.ledger import drain_tp_cascade
    drain_tp_cascade()
    # The global offline-scan has a CLASS-level 15-min cooldown; without a
    # reset, every test after the first silently skips the whole scan
    # (including PRE-COMMIT-RESOLVE) and fails confusingly.
    from engine.reconciler import StateReconciler
    StateReconciler._last_global_offline_scan = 0.0
    for attr in list(vars(StateReconciler)):
        if attr.startswith('_last_pair_scan_'):
            delattr(StateReconciler, attr)
    yield db_path
    if hasattr(database._local, 'connection') and database._local.connection:
        database._local.connection.close()
        database._local.connection = None
    from engine.ledger import _tp_cascade_registry
    drain_tp_cascade()


def _mkbot(conn, status="Scanning", direction="LONG"):
    conn.execute(
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, rsi_limit,"
        " martingale_multiplier, base_size, is_active, status)"
        " VALUES (?,?,?,?,?,?,?,?,1,?)",
        (BOT, "test-realpath", PAIR, "BTCUSDC", direction, 30, 1.88, 160.0, status),
    )
    conn.execute("INSERT INTO trades (bot_id, cycle_id) VALUES (?, ?)", (BOT, OLD_CYCLE))
    conn.commit()


def _seed_wedge(conn):
    """The exact pre-restart wedge state: old cycle's entry filled, TP row
    sitting in 'placing' (engine died before the fill arrived), trades row
    still showing the live position. Position qty (entry+grid) == TP qty, the
    real cycle-19 shape (0.002 entry + grids totaling 0.229 → TP 0.231)."""
    save_bot_order(
        BOT, "entry", "1200342118", 79269.8, 0.002, step=1, status="filled",
        client_order_id=f"CQB_{BOT}_ENTRY_{OLD_CYCLE}_1",
        notes="atomic-post-commit", cycle_id=OLD_CYCLE,
    )
    # Cycle-19 step-8 grid (the real last fill before the TP): makes
    # position qty == TP qty so the cascade reset path is the clean full-exit.
    save_bot_order(
        BOT, "grid", "1200734065", 78675.3, 0.229, step=8, status="filled",
        client_order_id=f"CQB_{BOT}_GRID_{OLD_CYCLE}_8",
        notes="grid filled before shutdown", cycle_id=OLD_CYCLE,
    )
    save_bot_order(
        BOT, "tp", TP_ORDER_ID, TP_PRICE, TP_QTY, step=8, status="placing",
        client_order_id=TP_CID, notes="placed before shutdown", cycle_id=OLD_CYCLE,
    )
    conn.execute(
        "UPDATE trades SET current_step=8, open_qty=0.231, total_invested=18159.59,"
        " avg_entry_price=78612.96, entry_confirmed=1, cycle_phase='ACTIVE',"
        " cycle_id=? WHERE bot_id=?",
        (OLD_CYCLE, BOT),
    )
    conn.commit()


def _seed_wedge_full_tp_placing(conn, tp_fill_ratio=1.0):
    """The full-TP wedge seed with a RATIO parameter for partial-fill tests.
    tp_fill_ratio < 1 leaves the position partially open (TP fill qty = ratio
    * full), matching a TP that only partially filled while the engine was
    down."""
    save_bot_order(
        BOT, "entry", "1200342118", 79269.8, 0.002, step=1, status="filled",
        client_order_id=f"CQB_{BOT}_ENTRY_{OLD_CYCLE}_1",
        notes="atomic-post-commit", cycle_id=OLD_CYCLE,
    )
    save_bot_order(
        BOT, "grid", "1200734065", 78675.3, TP_QTY - 0.002, step=8, status="filled",
        client_order_id=f"CQB_{BOT}_GRID_{OLD_CYCLE}_8",
        notes="grid filled before shutdown", cycle_id=OLD_CYCLE,
    )
    save_bot_order(
        BOT, "tp", TP_ORDER_ID, TP_PRICE, TP_QTY, step=8, status="placing",
        client_order_id=TP_CID, notes="placed before shutdown", cycle_id=OLD_CYCLE,
    )
    conn.execute(
        "UPDATE trades SET current_step=8, open_qty=?, total_invested=18159.59,"
        " avg_entry_price=78612.96, entry_confirmed=1, cycle_phase='ACTIVE',"
        " cycle_id=? WHERE bot_id=?",
        (TP_QTY, OLD_CYCLE, BOT),
    )
    conn.commit()


class TestRealPrecommitPath:
    def test_precommit_resolves_downtime_tp_via_real_code(self, tmp_db):
        from engine.reconciler import StateReconciler
        from engine.ledger import get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        _seed_wedge(conn)
        assert get_pending_tp_count() == 0

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _FakeExchange(_tp_fill_order())}
        rec._reconstruct_offline_fills_internal(since_hours=6)

        # (a) The TP row was credited by the real PRE-COMMIT-RESOLVE branch
        row = conn.execute(
            "SELECT status, filled_amount, order_id FROM bot_orders WHERE bot_id=? AND order_type='tp'",
            (BOT,),
        ).fetchone()
        assert row[0] == "filled", f"TP row must be credited, got {row[0]}"
        assert abs(float(row[1]) - TP_QTY) < 1e-9
        assert str(row[2]) == TP_ORDER_ID

        # (b) THE FIX: the cascade is registered for the downtime TP
        assert get_pending_tp_count() == 1, "downtime TP must register the TP cascade"

    def test_end_to_end_drain_advances_cycle(self, tmp_db):
        """Full replay: downtime TP → real PRE-COMMIT-RESOLVE credits + registers
        cascade → real handle_tp_completion drains it → cycle_id advances. This
        is the 09-07 four-bot + 09-09 10016 manual heal, automated."""
        from engine.reconciler import StateReconciler
        from engine.ledger import handle_tp_completion, get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        _seed_wedge(conn)

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _FakeExchange(_tp_fill_order())}
        rec._reconstruct_offline_fills_internal(since_hours=6)
        assert get_pending_tp_count() == 1

        ok = handle_tp_completion(
            bot_id=BOT, exit_price=TP_PRICE, pair=PAIR,
            exchange=_FakeExchange(position_qty=0.0), exit_fill_ts=TP_FILL_TS_S,
        )
        assert ok is True, "handle_tp_completion must succeed on the drained cascade"
        row = conn.execute(
            "SELECT cycle_id, close_type, open_qty FROM trades WHERE bot_id=?", (BOT,)
        ).fetchone()
        assert row[0] == NEW_CYCLE, f"cycle_id must advance to {NEW_CYCLE}, got {row[0]}"
        assert row[1] == "TP_HIT"
        assert abs(float(row[2] or 0)) < 1e-9

    def test_precommit_partial_grid_fill_credits_and_keeps_cycle_open(self, tmp_db):
        """Q1 (operator review): a GRID order that PARTIALLY fills during
        downtime must credit the partial amount, leave the cycle open, and
        register NO TP cascade — the cycle isn't closing, nothing to advance."""
        from engine.reconciler import StateReconciler
        from engine.ledger import get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        save_bot_order(
            BOT, "entry", "1200342118", 79269.8, 0.002, step=1, status="filled",
            client_order_id=f"CQB_{BOT}_ENTRY_{OLD_CYCLE}_1",
            notes="atomic-post-commit", cycle_id=OLD_CYCLE,
        )
        save_bot_order(
            BOT, "grid", None, 78550.0, 0.010, step=2, status="placing",
            client_order_id=f"CQB_{BOT}_GRID_{OLD_CYCLE}_2",
            notes="grid placing before shutdown", cycle_id=OLD_CYCLE,
        )
        conn.execute(
            "UPDATE trades SET current_step=2, open_qty=0.002, total_invested=158.5,"
            " avg_entry_price=79269.8, entry_confirmed=1, cycle_phase='ACTIVE',"
            " cycle_id=? WHERE bot_id=?",
            (OLD_CYCLE, BOT),
        )
        conn.commit()

        # Exchange truth: the grid partially filled (0.004 of 0.010), order still open
        partial_grid = {
            "id": "1200990001", "status": "open", "filled": 0.004,
            "amount": 0.010, "average": 78550.0, "price": 78550.0,
            "clientOrderId": f"CQB_{BOT}_GRID_{OLD_CYCLE}_2",
            "timestamp": 1788855730 * 1000, "lastTradeTimestamp": 1788855730 * 1000,
        }

        class _GridPartialEx(_FakeExchange):
            def fetch_order(self, cid, pair):
                if str(cid) == f"CQB_{BOT}_GRID_{OLD_CYCLE}_2":
                    return partial_grid
                raise KeyError(cid)

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _GridPartialEx()}
        rec._reconstruct_offline_fills_internal(since_hours=6)

        # (a) the partial amount is credited on the row
        row = conn.execute(
            "SELECT status, filled_amount, order_id FROM bot_orders "
            "WHERE client_order_id=?",
            (f"CQB_{BOT}_GRID_{OLD_CYCLE}_2",),
        ).fetchone()
        assert abs(float(row[1]) - 0.004) < 1e-9, f"partial grid fill must be credited, got {row[1]}"
        assert row[0] == "partially_filled", "in-progress partial maps to partially_filled (netting-recognized, matches in-session WS semantics)"
        assert str(row[2]) == "1200990001", "exchange order_id written back"

        # (b) NO TP cascade — the cycle is not closing
        assert get_pending_tp_count() == 0, "partial grid fill must not register a TP cascade"

        # (c) cycle state untouched: still open at the same cycle
        t = conn.execute(
            "SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id=?", (BOT,)
        ).fetchone()
        assert t[0] == OLD_CYCLE, "cycle_id must NOT advance on a partial grid fill"
        assert t[1] == "ACTIVE"
        assert abs(float(t[2]) - 0.006) < 1e-9, (
            f"partial credit must flow into trades.open_qty via seal "
            f"(entry 0.002 + partial grid 0.004 = 0.006), got {t[2]}"
        )

    def test_precommit_partial_tp_fill_credits_without_cascade(self, tmp_db):
        """Q2 (operator review): the dangerous variant — the TP itself PARTIALLY
        fills during downtime. Must credit the partial amount and register NO
        cascade: cascading would fire handle_tp_completion → open_qty>0 →
        PARTIAL_CLOSE_PENDING → force-close the remainder, which in-session
        semantics deliberately avoid (PARTIALLY_FILLED → credit-only). The
        cycle stays open; the remainder waits on the resting TP (re-placed by
        TP-SYNC on the next cycle if it expired)."""
        from engine.reconciler import StateReconciler
        from engine.ledger import get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        _seed_wedge_full_tp_placing(conn, tp_fill_ratio=0.5)

        # Exchange truth: TP half-filled (0.1155 of 0.231), order still open
        partial_tp = {
            "id": TP_ORDER_ID, "status": "open", "filled": 0.1155,
            "amount": TP_QTY, "average": TP_PRICE, "price": TP_PRICE,
            "clientOrderId": TP_CID,
            "timestamp": TP_FILL_TS_S * 1000, "lastTradeTimestamp": TP_FILL_TS_S * 1000,
        }

        class _TpPartialEx(_FakeExchange):
            def __init__(self):
                super().__init__()
            def fetch_order(self, cid, pair):
                if str(cid) == TP_CID:
                    return partial_tp
                raise KeyError(cid)

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _TpPartialEx()}
        rec._reconstruct_offline_fills_internal(since_hours=6)

        # (a) partial credit recorded on the row
        row = conn.execute(
            "SELECT status, filled_amount FROM bot_orders WHERE client_order_id=?", (TP_CID,)
        ).fetchone()
        assert abs(float(row[1]) - 0.1155) < 1e-9, f"partial TP fill must be credited, got {row[1]}"
        assert row[0] == "partially_filled", "in-progress partial TP maps to partially_filled"

        # (b) THE FIX-1 GATE: no cascade on partial
        assert get_pending_tp_count() == 0, "partial TP fill must NOT register the TP cascade"

        # (c) cycle state: not closed, not advanced
        t = conn.execute(
            "SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id=?", (BOT,)
        ).fetchone()
        assert t[0] == OLD_CYCLE, "cycle_id must NOT advance on partial TP fill"
        assert t[1] == "ACTIVE", "cycle stays open"
        assert float(t[2]) > 0, "position remainder still open"

    def test_precommit_cancel_expired_tp_with_partial_fill_no_cascade(self, tmp_db):
        """Q2 edge: GTX TPs EXPIRE. A TP that partially filled then EXPIRED
        while the engine was down surfaces here as canceled-with-fill. The
        Fix-5 semantics map it into this branch with o_filled>0 — it must
        credit the partial fill but NOT cascade (ratio < 0.99), matching the
        in-session cancel-after-partial semantic (keep accumulated, no cascade)."""
        from engine.reconciler import StateReconciler
        from engine.ledger import get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        _seed_wedge_full_tp_placing(conn, tp_fill_ratio=0.3)

        expired_partial_tp = {
            "id": TP_ORDER_ID, "status": "canceled", "filled": 0.0693,
            "amount": TP_QTY, "average": TP_PRICE, "price": TP_PRICE,
            "clientOrderId": TP_CID,
            "timestamp": TP_FILL_TS_S * 1000, "lastTradeTimestamp": TP_FILL_TS_S * 1000,
        }

        class _TpExpiredPartialEx(_FakeExchange):
            def fetch_order(self, cid, pair):
                if str(cid) == TP_CID:
                    return expired_partial_tp
                raise KeyError(cid)

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _TpExpiredPartialEx()}
        rec._reconstruct_offline_fills_internal(since_hours=6)

        row = conn.execute(
            "SELECT status, filled_amount FROM bot_orders WHERE client_order_id=?", (TP_CID,)
        ).fetchone()
        assert abs(float(row[1]) - 0.0693) < 1e-9, f"canceled-with-partial TP must credit the fill, got {row[1]}"
        assert get_pending_tp_count() == 0, "canceled-with-partial TP must NOT cascade"
        t = conn.execute("SELECT cycle_id, cycle_phase FROM trades WHERE bot_id=?", (BOT,)).fetchone()
        assert t[0] == OLD_CYCLE and t[1] == "ACTIVE"

    def test_precommit_no_cascade_for_non_tp(self, tmp_db):
        """Negative control: a downtime GRID fill must NOT register a TP cascade
        (only TP completions advance cycles)."""
        from engine.reconciler import StateReconciler
        from engine.ledger import get_pending_tp_count

        conn = get_connection()
        _mkbot(conn)
        save_bot_order(
            BOT, "grid", None, 78600.0, 0.002, step=2, status="placing",
            client_order_id=f"CQB_{BOT}_GRID_{OLD_CYCLE}_2", notes="grid before shutdown",
            cycle_id=OLD_CYCLE,
        )
        conn.execute(
            "UPDATE trades SET current_step=2, open_qty=0.004, total_invested=316.0,"
            " entry_confirmed=1, cycle_phase='ACTIVE', cycle_id=? WHERE bot_id=?",
            (OLD_CYCLE, BOT),
        )
        conn.commit()

        grid_fill = {
            "id": "1200734065", "status": "filled", "filled": 0.002,
            "amount": 0.002, "average": 78675.3, "price": 78675.3,
            "clientOrderId": f"CQB_{BOT}_GRID_{OLD_CYCLE}_2",
            "timestamp": 1788916800 * 1000, "lastTradeTimestamp": 1788916800 * 1000,
        }

        class _GridEx(_FakeExchange):
            def fetch_order(self, cid, pair):
                if str(cid) == f"CQB_{BOT}_GRID_{OLD_CYCLE}_2":
                    return grid_fill
                raise KeyError(cid)

        rec = StateReconciler.__new__(StateReconciler)
        rec.exchanges = {"future": _GridEx()}
        rec._reconstruct_offline_fills_internal(since_hours=6)

        assert get_pending_tp_count() == 0, "grid fills must not register TP cascades"
        row = conn.execute(
            "SELECT status FROM bot_orders WHERE bot_id=? AND order_type='grid'", (BOT,)
        ).fetchone()
        assert row[0] == "filled", "grid fill must still be credited normally"


class TestRealDedupGuardPath:
    @pytest.fixture(autouse=True)
    def _harness(self, monkeypatch, tmp_path):
        """The repo's established executor-test pattern (test_entry_dedup.py):
        real BotRunner + BotExecutor with mocked startup, temp DB, trading on.
        Patched parity gate: this test targets the DEDUP guard specifically."""
        import engine.database as database
        from unittest.mock import patch

        self.db_fd, self.db_temp = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = self.db_temp
        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        database.init_db()

        from config.settings import config as app_config
        self._orig_te, self._orig_dr = app_config.TRADING_ENABLED, app_config.DRY_RUN
        app_config.TRADING_ENABLED = True
        app_config.DRY_RUN = False

        from engine.runner import BotRunner
        from engine.bot_executor import BotExecutor
        with patch('engine.runner.startup.StartupMixin._initialize_exchanges'), \
             patch('engine.database.check_and_fix_integrity'), \
             patch('engine.migrations.migration_001_v2_schema.run'), \
             patch('engine.runner.startup.StartupMixin._post_init'):
            self.runner = BotRunner()
        self.fake = _FakeExchange()
        self.runner.exchanges = {'future': self.fake}
        self.runner.exchange = self.fake
        self.executor = BotExecutor(self.runner)

        import engine.parity_gates as pg
        monkeypatch.setattr(pg, "gate_trading_allowed", lambda bot_id, pair, ex: (True, "test-bypass"))

        yield

        if hasattr(database._local, 'connection') and database._local.connection:
            database._local.connection.close()
            database._local.connection = None
        database.DB_PATH = self.orig_db_path
        app_config.TRADING_ENABLED = self._orig_te
        app_config.DRY_RUN = self._orig_dr
        try:
            os.remove(self.db_temp)
        except Exception:
            pass

    def test_execute_entry_real_guard_self_heals(self):
        """The wedge state at next entry, through the REAL execute_entry:
        flat closed-cycle row + colliding filled ENTRY_19_1 → the fixed DEDUP
        guard self-advances and the entry places under cycle 20."""
        from engine.database import get_bot_status

        conn = get_connection()
        _mkbot(conn)
        save_bot_order(
            BOT, "entry", "1200342118", 79269.8, 0.002, step=1, status="filled",
            client_order_id=f"CQB_{BOT}_ENTRY_{OLD_CYCLE}_1",
            notes="atomic-post-commit", cycle_id=OLD_CYCLE,
        )
        conn.execute(
            "UPDATE trades SET current_step=0, open_qty=0, total_invested=0,"
            " avg_entry_price=0, target_tp_price=0, entry_confirmed=0,"
            " entry_order_id=NULL, tp_order_id=NULL, close_type='TP_HIT',"
            " cycle_phase='IDLE', cycle_id=? WHERE bot_id=?",
            (OLD_CYCLE, BOT),
        )
        conn.commit()

        bot_status = get_bot_status(BOT)
        bot_config = {"market_type": "future", "post_exit_stop": False}

        res = self.executor.execute_entry(
            bot_id=BOT, name="test-realpath", pair=PAIR, side="buy",
            amount=0.002, direction="LONG", price=78600.0, params={},
            exchange=self.fake, market_snapshot=None,
            bot_config=bot_config, bot_status=bot_status,
        )

        # The heal fired: cycle advanced in DB
        cyc = conn.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (BOT,)).fetchone()[0]
        assert cyc == NEW_CYCLE, f"guard must self-advance cycle to {NEW_CYCLE}, got {cyc}"

        # The entry PLACED under the new cycle (not blocked): the order row
        # exists in bot_orders with the new-cycle CID (real placement path
        # writes the row via the exchange seam).
        n = conn.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE client_order_id=?",
            (f"CQB_{BOT}_ENTRY_{NEW_CYCLE}_1",),
        ).fetchone()[0]
        assert n >= 1, "entry must place under the new-cycle CID"

    def test_execute_entry_blocks_when_live_position(self):
        """Negative control: the same collision shape but with a LIVE position
        (entry_confirmed=1, open_qty>0) must still BLOCK, not self-heal."""
        from engine.database import get_bot_status

        conn = get_connection()
        _mkbot(conn)
        save_bot_order(
            BOT, "entry", "1200342118", 79269.8, 0.002, step=1, status="filled",
            client_order_id=f"CQB_{BOT}_ENTRY_{OLD_CYCLE}_1",
            notes="atomic-post-commit", cycle_id=OLD_CYCLE,
        )
        conn.execute(
            "UPDATE trades SET current_step=2, open_qty=0.004, total_invested=314.0,"
            " entry_confirmed=1, cycle_phase='ACTIVE', cycle_id=? WHERE bot_id=?",
            (OLD_CYCLE, BOT),
        )
        conn.commit()

        bot_status = get_bot_status(BOT)
        bot_config = {"market_type": "future", "post_exit_stop": False}

        res = self.executor.execute_entry(
            bot_id=BOT, name="test-realpath", pair=PAIR, side="buy",
            amount=0.002, direction="LONG", price=78600.0, params={},
            exchange=self.fake, market_snapshot=None,
            bot_config=bot_config, bot_status=bot_status,
        )

        cyc = conn.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (BOT,)).fetchone()[0]
        assert cyc == OLD_CYCLE, "live position must NOT self-advance"
        n = conn.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE client_order_id=? AND bot_id=?",
            (f"CQB_{BOT}_ENTRY_{NEW_CYCLE}_1", BOT),
        ).fetchone()[0]
        assert n == 0, "no new-cycle order row may be written when blocked"
