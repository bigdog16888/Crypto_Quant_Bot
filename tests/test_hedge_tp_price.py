import pytest
from engine.ledger import _calc_hedge_tp_price

def test_hedge_tp_price_profitable_short():
    # entry=0.85, current=0.75 -> TP price=0.75 (profitable SHORT)
    price = _calc_hedge_tp_price(direction='SHORT', avg_entry_price=0.85, current_price=0.75)
    assert price == 0.75

def test_hedge_tp_price_loss_short():
    # entry=0.75, current=0.85 -> TP price=0.75 (losing SHORT - wait for break-even)
    price = _calc_hedge_tp_price(direction='SHORT', avg_entry_price=0.75, current_price=0.85)
    assert price == 0.75

def test_hedge_tp_price_profitable_long():
    # entry=1.50, current=1.70 -> TP price=1.70 (profitable LONG)
    price = _calc_hedge_tp_price(direction='LONG', avg_entry_price=1.50, current_price=1.70)
    assert price == 1.70

def test_hedge_tp_price_loss_long():
    # entry=1.70, current=1.50 -> TP price=1.70 (losing LONG - wait for break-even)
    price = _calc_hedge_tp_price(direction='LONG', avg_entry_price=1.70, current_price=1.50)
    assert price == 1.70
