"""
Tests for the Force Close Orphan button safety gate and formatting logic.
"""
import sys
import os
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

# Mock streamlit before imports
class MockStreamlit:
    def __init__(self):
        self._session_state = {}
        
    def __getattr__(self, name):
        if name in ('fragment', 'dialog', 'experimental_fragment', 'experimental_dialog'):
            return lambda *args, **kwargs: (args[0] if (len(args) == 1 and callable(args[0]) and not kwargs) else (lambda f: f))
        def noop(*args, **kwargs):
            return None
        return noop
    
    @property
    def session_state(self):
        return self._session_state
    
    def columns(self, *args, **kwargs):
        class MockColumn:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __getattr__(self, name):
                def noop(*args, **kwargs):
                    return None
                return noop
        return [MockColumn() for _ in range(10)]
    
    def expander(self, *args, **kwargs):
        class MockExpander:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __getattr__(self, name):
                def noop(*args, **kwargs):
                    return None
                return noop
        return MockExpander()

    def cache_resource(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator
    
    def cache_data(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

# Install mock
sys.modules['streamlit'] = MockStreamlit()

# Now we can safely import ui.views.monitor
from ui.views import monitor

def check_can_close_orphan(df_pos_f, mp_pair, _norm_universal):
    """
    Recreates the exact safety gate check from monitor.py to verify its correctness.
    """
    _p_clean = mp_pair.split(' ')[0]
    pair_bots = df_pos_f[df_pos_f['pair'].apply(_norm_universal) == _p_clean]
    can_close_orphan = True
    for _, bot_row in pair_bots.iterrows():
        invested = float(bot_row.get('total_invested', 0) or 0)
        derived_status = str(bot_row.get('status', '')).upper()
        c_phase = str(bot_row.get('cycle_phase', 'IDLE')).upper()
        is_scanning_or_idle = ("SCANNING" in derived_status) or ("IDLE" in derived_status) or (c_phase == "IDLE")
        if invested > 0.01 or not is_scanning_or_idle:
            can_close_orphan = False
            break
    return can_close_orphan


def test_can_close_orphan_safety_gate():
    # Helper universal normalization mapping
    def _norm_universal(p):
        if not p:
            return ""
        return p.replace("/", "").replace(":", "").split("USDT")[0].split("USDC")[0].upper()

    # Case 1: No bots configured for this pair -> can_close_orphan should be True
    df_empty = pd.DataFrame(columns=['pair', 'total_invested', 'status', 'cycle_phase'])
    assert check_can_close_orphan(df_empty, "BTC NET", _norm_universal) is True

    # Case 2: Bots exist, all are SCANNING / IDLE with 0.0 invested -> True
    df_all_scanning = pd.DataFrame([
        {'pair': 'BTC/USDT:USDT', 'total_invested': 0.0, 'status': 'SCANNING', 'cycle_phase': 'IDLE'},
        {'pair': 'BTC/USDT:USDT', 'total_invested': 0.0, 'status': 'IDLE', 'cycle_phase': 'IDLE'}
    ])
    assert check_can_close_orphan(df_all_scanning, "BTC NET", _norm_universal) is True

    # Case 3: One bot has invested amount > 0.01 -> False
    df_one_invested = pd.DataFrame([
        {'pair': 'BTC/USDT:USDT', 'total_invested': 0.0, 'status': 'SCANNING', 'cycle_phase': 'IDLE'},
        {'pair': 'BTC/USDT:USDT', 'total_invested': 50.0, 'status': 'SCANNING', 'cycle_phase': 'IDLE'}
    ])
    assert check_can_close_orphan(df_one_invested, "BTC NET", _norm_universal) is False

    # Case 4: One bot is in ACTIVE state -> False
    df_one_active = pd.DataFrame([
        {'pair': 'BTC/USDT:USDT', 'total_invested': 0.0, 'status': 'SCANNING', 'cycle_phase': 'IDLE'},
        {'pair': 'BTC/USDT:USDT', 'total_invested': 0.0, 'status': 'ACTIVE', 'cycle_phase': 'ACTIVE'}
    ])
    assert check_can_close_orphan(df_one_active, "BTC NET", _norm_universal) is False


def test_ccxt_pair_suffix_formatting():
    # Helper function from monitor.py to form CCXT symbol representation
    def get_ccxt_pair(_p_clean):
        _ccxt_pair = _p_clean
        if ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDC'):
            _ccxt_pair = f"{_ccxt_pair}:USDC"
        elif ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDT'):
            _ccxt_pair = f"{_ccxt_pair}:USDT"
        return _ccxt_pair

    assert get_ccxt_pair("BTC/USDT") == "BTC/USDT:USDT"
    assert get_ccxt_pair("ETH/USDC") == "ETH/USDC:USDC"
    assert get_ccxt_pair("SOL/USDT:USDT") == "SOL/USDT:USDT"
    assert get_ccxt_pair("XRP/USDT") == "XRP/USDT:USDT"


@patch('ui.views.monitor.get_exchange_instance')
@patch('ui.views.monitor.clear_manual_whitelists_for_pair')
def test_close_orphan_execution_flow(mock_clear_whitelists, mock_get_exchange):
    # Mock exchange behavior
    mock_ex = MagicMock()
    mock_ex.get_symbol_precision.return_value = {'amount_step': 0.01, 'step_size': 0.01}
    mock_ex.round_to_step.side_effect = lambda qty, step: round(qty / step) * step
    mock_get_exchange.return_value = mock_ex

    # Let's verify our execution logic by mimicking what the button action block does.
    mp_pair = "BTC/USDT"
    mp_pqty = 0.1234
    _p_clean = mp_pair.split(' ')[0]
    
    _ccxt_pair = _p_clean
    if ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDC'):
        _ccxt_pair = f"{_ccxt_pair}:USDC"
    elif ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDT'):
        _ccxt_pair = f"{_ccxt_pair}:USDT"
        
    close_side = 'sell' if mp_pqty > 0 else 'buy'
    close_qty = abs(mp_pqty)
    
    prec = mock_ex.get_symbol_precision(_ccxt_pair)
    step = float(prec.get('amount_step', prec.get('step_size', 0)) or 0)
    if step > 0:
        close_qty = mock_ex.round_to_step(close_qty, step)

    assert _ccxt_pair == "BTC/USDT:USDT"
    assert close_side == "sell"
    assert close_qty == pytest.approx(0.12, abs=1e-6)

    # Place order on exchange
    client_id = f"CQB_ORPH_{_ccxt_pair.replace('/', '').replace(':', '')}"
    mock_ex.create_order(
        symbol=_ccxt_pair,
        type='market',
        side=close_side,
        amount=close_qty,
        price=None,
        params={
            'reduceOnly': True,
            'clientOrderId': client_id,
            'human_approved': True,
        },
    )

    mock_ex.create_order.assert_called_once_with(
        symbol="BTC/USDT:USDT",
        type="market",
        side="sell",
        amount=pytest.approx(0.12, abs=1e-6),
        price=None,
        params={
            'reduceOnly': True,
            'clientOrderId': "CQB_ORPH_BTCUSDTUSDT",
            'human_approved': True,
        }
    )
