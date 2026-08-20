#!/usr/bin/env python3
"""
O-2 test: _exchange_sync_diagnostics_fragment() must consume
st.session_state["system_health_data"]["netting_status_per_pair"]
(the same source as the header status pill), NOT the
data/exchange_sync_diagnostics.json cache file.

Proof strategy:
  1. Fabricate session_state["system_health_data"] with a synthetic
     drifting pair (values chosen to be unambiguous).
  2. Overwrite the real JSON cache file with DIFFERENT values
     (a "poison" sentinel) for the same pair.
  3. Run the fragment with a streamlit mock that captures rendered
     markdown / expander / dataframe output.
  4. Assert the rendered output matches health_data values and does
     NOT contain the poisoned JSON values.
  5. Restore the original JSON cache file in a finally block.
"""
import os
import sys
import json
import types

# Ensure repo root is importable (conftest does env setup; mirror it here)
os.environ.setdefault("TESTING_MODE", "True")
os.environ.setdefault("PYTEST_RUNNING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Capturing streamlit mock (extends the pattern from test_streamlit_smoke.py)
# ---------------------------------------------------------------------------
class _MockExpander:
    def __init__(self, recorder, label, expanded):
        self._recorder = recorder
        self._label = label
        self._expanded = expanded

    def __enter__(self):
        self._recorder["expanders"].append({"label": self._label, "expanded": self._expanded})
        return self

    def __exit__(self, *args):
        return False


class _MockStreamlit:
    def __init__(self):
        self._session_state = {}
        self.records = {"markdown": [], "info": [], "error": [], "warn": [],
                        "expanders": [], "dataframes": []}

    def __getattr__(self, name):
        if name in ("fragment", "dialog", "experimental_fragment", "experimental_dialog"):
            def deco(*args, **kwargs):
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                return lambda f: f
            return deco
        if name in ("cache_resource", "cache_data"):
            def cache_deco(*args, **kwargs):
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                return lambda f: f
            return cache_deco
        if name == "markdown":
            return lambda *a, **k: self.records["markdown"].extend(a)
        if name == "info":
            return lambda *a, **k: self.records["info"].extend(a)
        if name in ("error", "warning"):
            return lambda *a, **k: self.records["error" if name == "error" else "warn"].extend(a)
        if name == "expander":
            def expander(label, expanded=False):
                return _MockExpander(self.records, label, expanded)
            return expander
        if name == "dataframe":
            return lambda df, **k: self.records["dataframes"].append(df)
        def noop(*args, **kwargs):
            return None
        return noop

    @property
    def session_state(self):
        return self._session_state


def _install_mock():
    mock = _MockStreamlit()
    sys.modules["streamlit"] = mock
    return mock


def _make_health_data():
    """Fabricated health_data exactly as engine/health.py compute_system_health()
    returns it (see engine/health.py lines 236-241 for the per-pair schema)."""
    return {
        "timestamp": 1760000000.0,
        "startup_suppression": False,
        "startup_remaining_s": 0.0,
        "engine_started_at": 1759999900.0,
        "system_status": "MISMATCH",
        "worst_gap_usd": 133500.0,
        "mismatched_pair_count": 1,
        "netting_status_per_pair": {
            "BTCUSDT": {
                "pair": "BTCUSDT",
                "virtual_net": 0.0,
                "physical_net": 6.675,          # sentinel: 6.675
                "diff_qty": 6.675,
                "diff_usd": 133500.0,           # 6.675 * 20000
                "drift_detected": True,
                "ref_price": 20000.0,
                "tolerance": 0.002,
                "bots": [
                    {"bot_id": 10001, "name": "btc-long-A", "direction": "LONG",
                     "open_qty": 0.0, "avg_price": 0.0},
                ],
            },
            "SOLUSDC": {
                "pair": "SOLUSDC",
                "virtual_net": 100.0,
                "physical_net": 100.0,
                "diff_qty": 0.0,
                "diff_usd": 0.0,
                "drift_detected": False,
                "ref_price": 150.0,
                "tolerance": 0.002,
                "bots": [],
            },
        },
        "order_health": {"status_color": "green", "message": "ok", "bot_statuses": {}},
        "header_metrics": {},
        "orphan_positions": [],
        "stuck_cascade_bots": [],
        "manual_proof_bots": [],
        "dust_bots": [],
    }


# Poison sentinel values: deliberately different from health_data so any
# accidental JSON read is detectable in the rendered output.
POISON = {
    "BTCUSDT": {
        "timestamp": 1, "pair": "BTCUSDT",
        "exchange_net": -1234.5,   # sentinel: -1234.5
        "db_sum_qty": 999.9,       # sentinel: 999.9
        "diff": 1.0, "tolerance": 0.002,
        "drift_detected": True,
        "bots": [{"bot_id": 1, "name": "POISONBOT", "direction": "LONG",
                  "open_qty": 42.0, "signed_qty": 42.0}],
    }
}


def _cache_path():
    from config.settings import config
    return os.path.join(config.ROOT_DIR, "data", "exchange_sync_diagnostics.json")


def _reload_monitor():
    """Re-import ui.views.monitor so its `st` binding points at the FRESH mock.
    Must purge the whole package chain: `from ui.views import monitor` otherwise
    returns the stale module via the package attribute."""
    for m in list(sys.modules):
        if m == "ui" or m == "ui.views" or m.startswith("ui.views.monitor"):
            del sys.modules[m]
    from ui.views import monitor
    return monitor


def test_o2_fragment_reads_health_data_not_json_cache():
    mock = _install_mock()

    monitor = _reload_monitor()

    cache = _cache_path()
    original = None
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            original = f.read()
    try:
        # Poison the JSON cache with sentinel values
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(POISON, f, indent=4)

        # Fabricated session state (as render_monitor_view would store it)
        mock.session_state["system_health_data"] = _make_health_data()

        monitor._exchange_sync_diagnostics_fragment()

        md = "\n".join(str(x) for x in mock.records["markdown"])
        expander_labels = [e["label"] for e in mock.records["expanders"]]

        # 1. Header pill source data drives the panel: drifting pair surfaced
        assert any("DRIFT DETECTED" in m for m in mock.records["markdown"]), "drift banner missing"
        assert "BTCUSDT" in md, "drifting pair name missing"

        # 2. Values match health_data (physical_net / virtual_net / diff_usd)
        assert "`6.67500000`" in md, "physical_net (exchange_net) from health_data not rendered"
        assert "`0.00000000`" in md, "virtual_net (db net) from health_data not rendered"
        assert "`+6.67500000`" in md, "signed diff not rendered"
        assert "$133,500.00" in md, "diff_usd not rendered"
        assert "**Tolerance:** 0.002000" in md

        # 3. Expander label counts only the drifting pair
        assert "Exchange Sync Diagnostics (1 pairs drifting)" in " | ".join(expander_labels), expander_labels

        # 4. JSON cache path is DEAD: no sentinel value leaked into output
        assert "-1234" not in md and "1234.5" not in md, "poisoned JSON exchange_net leaked"
        assert "999.9" not in md, "poisoned JSON db_sum_qty leaked"
        assert "POISONBOT" not in md, "poisoned JSON bots leaked"

        # 5. Bot table: signed_qty computed from direction (LONG 0.0 -> +0.0)
        dframes = mock.records["dataframes"]
        assert len(dframes) == 1, f"expected 1 bot dataframe, got {len(dframes)}"
        row = dframes[0].iloc[0].to_dict()
        assert row["Bot ID"] == 10001
        assert row["Signed Qty (Contribution)"] == 0.0
    finally:
        if original is not None:
            with open(cache, "wb") as f:
                f.write(original)
        elif os.path.exists(cache):
            os.remove(cache)


def test_o2_fragment_shorts_get_negative_signed_qty():
    mock = _install_mock()
    monitor = _reload_monitor()

    hd = _make_health_data()
    hd["netting_status_per_pair"]["BTCUSDT"]["bots"] = [
        {"bot_id": 10002, "name": "btc-short-A", "direction": "SHORT",
         "open_qty": 0.05, "avg_price": 20000.0},
    ]
    mock.session_state["system_health_data"] = hd

    monitor._exchange_sync_diagnostics_fragment()

    dframes = mock.records["dataframes"]
    assert len(dframes) == 1
    row = dframes[0].iloc[0].to_dict()
    assert row["Signed Qty (Contribution)"] == -0.05, row


def test_o2_fragment_all_in_sync():
    mock = _install_mock()
    monitor = _reload_monitor()

    hd = _make_health_data()
    del hd["netting_status_per_pair"]["BTCUSDT"]  # only in-sync SOLUSDC remains
    mock.session_state["system_health_data"] = hd

    monitor._exchange_sync_diagnostics_fragment()

    md = "\n".join(str(x) for x in mock.records["markdown"])
    assert "All pairs in sync" in md
    labels = " | ".join(e["label"] for e in mock.records["expanders"])
    assert "pairs drifting" not in labels, labels


def test_o2_fragment_no_health_data_yet():
    mock = _install_mock()
    monitor = _reload_monitor()

    mock.session_state.clear()
    monitor._exchange_sync_diagnostics_fragment()

    assert any("No exchange sync diagnostics data available yet" in str(x)
               for x in mock.records["info"]), mock.records["info"]


def test_o2_source_no_longer_reads_json_cache():
    """Source-level guard: the function body must not contain JSON-cache reads."""
    import inspect
    mock = _install_mock()
    monitor = _reload_monitor()

    src = inspect.getsource(monitor._exchange_sync_diagnostics_fragment)
    assert "json.load" not in src
    assert "exchange_sync_diagnostics.json" not in src.replace(
        "Previously read data/exchange_sync_diagnostics.json", "")
    assert "st.session_state.get(\"system_health_data\")" in src
    assert "netting_status_per_pair" in src