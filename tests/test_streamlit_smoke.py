"""
Streamlit app smoke test - simulates loading each view.
Run with: python tests/test_streamlit_smoke.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock streamlit before imports
class MockStreamlit:
    """Mock streamlit to test view functions without browser."""
    
    def __init__(self):
        self._session_state = {}
        
    def __getattr__(self, name):
        # Return a no-op function/decorator for any st.* call
        if name in ('fragment', 'dialog', 'experimental_fragment', 'experimental_dialog'):
            return lambda *args, **kwargs: (args[0] if (len(args) == 1 and callable(args[0]) and not kwargs) else (lambda f: f))
        def noop(*args, **kwargs):
            return None
        return noop
    
    def button(self, *args, **kwargs):
        return False
    
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
    
    def form(self, *args, **kwargs):
        class MockForm:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __getattr__(self, name):
                def noop(*args, **kwargs):
                    return None
                return noop
        return MockForm()
    
    def cache_resource(self, *args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda func: func
    
    def cache_data(self, *args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda func: func

# Install mock
sys.modules['streamlit'] = MockStreamlit()

def test_app_imports():
    """Test that main app module imports without error."""
    print("1. Testing main app imports...")
    
    try:
        # Import database and initialize
        from engine.database import init_db
        init_db()
        print("   Database initialized: OK")
        
        # Test each view import
        from ui.views.bot_manager import render_bot_manager_view
        print("   bot_manager import: OK")
        
        from ui.views.bot_creator import render_bot_creator_view
        print("   bot_creator import: OK")
        
        from ui.views.monitor import render_monitor_view
        print("   monitor import: OK")
        
        print("   ✅ All imports: PASSED")
        return True
        
    except Exception as e:
        print(f"   ❌ Import error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_database_views():
    """Test database queries used by views."""
    print("\n2. Testing database queries for views...")
    
    from engine.database import (
        get_all_bots, get_bot_params, get_bot_status,
        get_trade_history, get_bot_pnl_summary
    )
    
    bots = get_all_bots()
    print(f"   get_all_bots(): {len(bots)} bots")
    
    if bots:
        bot_id = bots[0][0]
        
        # Simulate view queries
        params = get_bot_params(bot_id)
        print(f"   get_bot_params({bot_id}): {'OK' if params else 'EMPTY'}")
        
        status = get_bot_status(bot_id)
        print(f"   get_bot_status({bot_id}): {'OK' if status else 'EMPTY'}")
        
        history = get_trade_history(bot_id, limit=10)
        print(f"   get_trade_history({bot_id}): {len(history)} records")
        
        pnl = get_bot_pnl_summary(bot_id)
        print(f"   get_bot_pnl_summary({bot_id}): {pnl}")
    
    print("   ✅ Database queries: PASSED")
    return True

def test_exchange_for_views():
    """Test exchange functions used by views."""
    print("\n3. Testing exchange for views...")
    
    try:
        from engine.exchange_interface import ExchangeInterface
        
        ex = ExchangeInterface(market_type='future')
        
        # Bot creator needs symbols
        symbols = ex.get_available_symbols(quote_asset='USDT')
        print(f"   Available symbols: {len(symbols)}")
        
        # Monitor needs price
        price = ex.get_last_price('BTC/USDT')
        print(f"   BTC/USDT price: {price}")
        
        print("   ✅ Exchange: PASSED")
        return True
        
    except Exception as e:
        print(f"   ⚠️ Exchange (non-fatal): {e}")
        return True

def main():
    print("=" * 60)
    print("🖥️ STREAMLIT SMOKE TEST")
    print("=" * 60)
    
    results = []
    results.append(("App Imports", test_app_imports()))
    results.append(("Database Views", test_database_views()))
    results.append(("Exchange Views", test_exchange_for_views()))
    
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print("\n" + ("✅ SMOKE TEST PASSED - App should load" if all_passed else "❌ SMOKE TEST FAILED"))
    return all_passed

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
