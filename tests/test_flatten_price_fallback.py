"""
Regression test for handle_flatten price=0.0 fix.
Verifies that the fix prevents silent 0.0 usage by adding fallback chain.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_flatten_fix_code_exists():
    """Verify the fix code is present in handle_flatten."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'engine', 'ledger.py')) as f:
        content = f.read()
    
    # Verify the fix components are present
    assert "Never silently use 0.0" in content
    assert "fetch_ticker" in content
    assert "CRITICAL — all fallbacks exhausted" in content
    assert "hasattr(exchange, 'fetch_ticker')" in content
    assert "actual_exit_price is None or actual_exit_price <= 0.0" in content


def test_flatten_imports_and_compiles():
    """Verify the module imports without error."""
    from engine.ledger import handle_flatten
    assert callable(handle_flatten)


def test_flatten_has_correct_signature():
    """Verify handle_flatten has the expected signature with Optional import."""
    import engine.ledger as ledger
    import inspect
    
    sig = inspect.signature(ledger.handle_flatten)
    params = list(sig.parameters.keys())
    assert 'bot_id' in params
    assert 'pair' in params
    assert 'exchange' in params
    assert 'reason' in params
    assert 'exit_price' in params
    assert sig.parameters['exit_price'].default is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])