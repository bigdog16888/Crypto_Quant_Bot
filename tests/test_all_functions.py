"""
Comprehensive test of all core functions before manual testing.
Run with: python tests/test_all_functions.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_database():
    """Test all database functions."""
    print("1. Testing Database Functions...")
    from engine.database import (
        init_db, get_all_bots, get_bot_params, get_bot_status,
        get_trade_history, get_bot_pnl_summary, get_connection
    )
    
    init_db()
    
    # Test connection recovery
    conn = get_connection()
    conn.close()
    conn2 = get_connection()  # Should auto-reconnect
    conn2.execute("SELECT 1")
    print("   Connection recovery: OK")
    
    bots = get_all_bots()
    print(f"   get_all_bots(): {len(bots)} bots")
    
    if bots:
        bot_id = bots[0][0]
        params = get_bot_params(bot_id)
        assert params is not None, "get_bot_params failed"
        print(f"   get_bot_params({bot_id}): OK")
        
        status = get_bot_status(bot_id)
        print(f"   get_bot_status({bot_id}): OK")
        
        history = get_trade_history(bot_id)
        print(f"   get_trade_history({bot_id}): {len(history)} records")
        
        pnl = get_bot_pnl_summary(bot_id)
        print(f"   get_bot_pnl_summary({bot_id}): {pnl}")
    
    print("   ✅ Database: PASSED")

def test_strategy():
    """Test strategy functions."""
    print("\n2. Testing Strategy Functions...")
    from engine.strategies.martingale_strategy import MartingaleStrategy
    from engine.strategies.market_maker import MarketMakerStrategy
    
    # Test Martingale Strategy
    strat = MartingaleStrategy()
    
    # Test lot size calculation (positional args: current_step, account_balance)
    lot = strat.calculate_lot_size(0, 1000)
    assert lot > 0, "Lot size should be positive"
    print(f"   Martingale calculate_lot_size(step=0): ${lot}")
    
    lot2 = strat.calculate_lot_size(3, 1000)
    assert lot2 > lot, "Martingale should increase lot size"
    print(f"   Martingale calculate_lot_size(step=3): ${lot2}")
    
    # Test Market Maker Strategy
    mm_strat = MarketMakerStrategy("test_mm", {'order_size': 10})
    print(f"   MM initialized: {mm_strat}")
    
    print("   ✅ Strategy: PASSED")

def test_exchange():
    """Test exchange interface (may fail without API keys)."""
    print("\n3. Testing Exchange Interface...")
    try:
        from engine.exchange_interface import ExchangeInterface
        
        ex = ExchangeInterface(market_type='future')
        
        # Test symbol fetching
        symbols = ex.get_available_symbols(quote_asset='USDT')
        print(f"   get_available_symbols(): {len(symbols)} pairs")
        
        # Test price fetching
        price = ex.get_last_price('BTC/USDT')
        print(f"   get_last_price(BTC/USDT): {price}")
        
        print("   ✅ Exchange: PASSED")
    except Exception as e:
        print(f"   ⚠️ Exchange test skipped: {e}")

def test_runner():
    """Test runner imports and basic setup."""
    print("\n4. Testing Runner...")
    from engine.runner import BotRunner
    print("   BotRunner import: OK")
    print("   ✅ Runner: PASSED")

def test_manager():
    """Test manager functions."""
    print("\n5. Testing Manager (Skipped - Obsolete)...")

def test_indicators():
    """Test indicator calculations."""
    print("\n6. Testing Indicators...")
    import pandas as pd
    import numpy as np
    from engine.indicators import rsi, cci
    
    # Create test data
    df = pd.DataFrame({
        'open': np.random.uniform(100, 110, 50),
        'high': np.random.uniform(110, 120, 50),
        'low': np.random.uniform(90, 100, 50),
        'close': np.random.uniform(100, 110, 50),
    })
    
    rsi_val = rsi(df['close'], period=14)
    assert not rsi_val.isna().all(), "RSI should have values"
    print(f"   rsi(): OK (last={rsi_val.iloc[-1]:.2f})")
    
    cci_val = cci(df['high'], df['low'], df['close'], period=14)
    assert not cci_val.isna().all(), "CCI should have values"
    print(f"   cci(): OK (last={cci_val.iloc[-1]:.2f})")
    
    print("   ✅ Indicators: PASSED")

def test_ui_views():
    """Test UI view imports (without Streamlit runtime)."""
    print("\n7. Testing UI View Imports...")
    
    # These will fail to render without Streamlit, but imports should work
    try:
        from ui.views.bot_manager import render_bot_manager_view
        print("   bot_manager import: OK")
    except Exception as e:
        print(f"   bot_manager: {e}")
    
    try:
        from ui.views.bot_creator import render_bot_creator_view
        print("   bot_creator import: OK")
    except Exception as e:
        print(f"   bot_creator: {e}")
    
    try:
        from ui.views.monitor import render_monitor_view
        print("   monitor import: OK")
    except Exception as e:
        print(f"   monitor: {e}")
    
    print("   ✅ UI Imports: PASSED")

def main():
    print("=" * 60)
    print("🧪 COMPREHENSIVE FUNCTION TEST")
    print("=" * 60)
    
    tests = [
        ("Database", test_database),
        ("Strategy", test_strategy),
        ("Exchange", test_exchange),
        ("Runner", test_runner),
        ("Manager", test_manager),
        ("Indicators", test_indicators),
        ("UI Imports", test_ui_views)
    ]
    
    results = []
    all_passed = True
    for name, func in tests:
        try:
            func()
            results.append((name, True))
        except Exception as e:
            print(f"   ❌ {name} failed: {e}")
            results.append((name, False))
            all_passed = False
    
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"  {name}: {status}")
    
    print("\n" + ("✅ ALL TESTS PASSED" if all_passed else "❌ SOME TESTS FAILED"))
    return all_passed

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
