#!/usr/bin/env python3
"""
Deep Account Health & Leverage Diagnostic
Checks account balance, positions, margin usage, and leverage configuration
"""

import sys
import sqlite3
from pathlib import Path
import ccxt
from dotenv import load_dotenv
import os

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Load environment variables
load_dotenv()

def format_number(value, decimals=2):
    """Format number with commas and decimals"""
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}"

def main():
    print("="*80)
    print("DEEP ACCOUNT HEALTH & LEVERAGE DIAGNOSTIC")
    print("="*80)
    print()
    
    # ========================================
    # 1. LOAD BOT CONFIGURATIONS FROM DATABASE
    # ========================================
    print("[1/5] Loading bot configurations from database...")
    db_path = project_root / "crypto_bot.db"
    
    if not db_path.exists():
        print(f"❌ ERROR: Database not found at {db_path}")
        return 1
    
    bots_config = {}
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get bot configurations
        cursor.execute("SELECT name, config FROM bots WHERE is_active = 1")
        rows = cursor.fetchall()
        
        if not rows:
            print("❌ No active bots found in database")
            return 1
        
        import json
        for name, config_json in rows:
            config = json.loads(config_json)
            bots_config[name] = config
            leverage = config.get('params', {}).get('leverage', 1)
            symbol = config.get('pair', 'UNKNOWN')
            print(f"  ✓ {name}: {symbol} - Configured Leverage: {leverage}x")
        
        conn.close()
        print(f"✓ Loaded {len(bots_config)} active bot(s)")
    except Exception as e:
        print(f"❌ Database error: {e}")
        return 1
    
    print()
    
    # ========================================
    # 2. INITIALIZE EXCHANGE CONNECTION
    # ========================================
    print("[2/5] Connecting to Binance Futures...")
    
    use_testnet = os.getenv('USE_TESTNET', 'True').lower() == 'true'
    
    if use_testnet:
        api_key = os.getenv('BINANCE_TESTNET_API_KEY')
        api_secret = os.getenv('BINANCE_TESTNET_API_SECRET')
        print("  Mode: TESTNET")
    else:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        print("  Mode: MAINNET ⚠️")
    
    if not api_key or not api_secret:
        print("❌ ERROR: API credentials not found in .env file")
        return 1
    
    try:
        exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
            'enableRateLimit': True,
        })
        
        if use_testnet:
            exchange.set_sandbox_mode(True)
        
        # Test connection
        exchange.load_markets()
        print("✓ Connected to Binance Futures API")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return 1
    
    print()
    
    # ========================================
    # 3. FETCH ACCOUNT BALANCE & MARGIN
    # ========================================
    print("[3/5] Fetching account balance and margin...")
    
    try:
        balance = exchange.fetch_balance()
        
        # Extract futures account info
        info = balance.get('info', {})
        
        # For Binance Futures, balance info is in 'assets' or direct in 'info'
        total_wallet_balance = 0
        total_unrealized_pnl = 0
        total_margin_balance = 0
        available_balance = 0
        
        if 'totalWalletBalance' in info:
            total_wallet_balance = float(info.get('totalWalletBalance', 0))
            total_unrealized_pnl = float(info.get('totalUnrealizedProfit', 0))
            total_margin_balance = float(info.get('totalMarginBalance', 0))
            available_balance = float(info.get('availableBalance', 0))
        else:
            # Fallback to USDT balance
            usdt = balance.get('USDT', {})
            total_wallet_balance = usdt.get('total', 0)
            available_balance = usdt.get('free', 0)
            total_margin_balance = total_wallet_balance
        
        used_margin = total_margin_balance - available_balance
        
        print()
        print("  📊 ACCOUNT BALANCE SUMMARY")
        print("  " + "-"*76)
        print(f"  Total Wallet Balance:     ${format_number(total_wallet_balance)}")
        print(f"  Unrealized P&L:           ${format_number(total_unrealized_pnl)}")
        print(f"  Total Margin Balance:     ${format_number(total_margin_balance)}")
        print(f"  Used Margin:              ${format_number(used_margin)}")
        print(f"  Available Balance (Free): ${format_number(available_balance)}")
        print("  " + "-"*76)
        
        if used_margin > 0:
            margin_usage_pct = (used_margin / total_margin_balance) * 100
            print(f"  Margin Usage:             {margin_usage_pct:.2f}%")
        else:
            print(f"  Margin Usage:             0.00%")
        
    except Exception as e:
        print(f"❌ Failed to fetch balance: {e}")
        return 1
    
    print()
    
    # ========================================
    # 4. FETCH OPEN POSITIONS & LEVERAGE
    # ========================================
    print("[4/5] Fetching open positions and leverage settings...")
    
    try:
        positions = exchange.fetch_positions()
        
        # Filter only positions with size > 0
        open_positions = [p for p in positions if float(p.get('contracts', 0)) > 0]
        
        if not open_positions:
            print("  ℹ️  No open positions found")
        else:
            print()
            print("  📈 OPEN POSITIONS")
            print("  " + "-"*76)
            
            for pos in open_positions:
                symbol = pos.get('symbol', 'UNKNOWN')
                side = pos.get('side', 'UNKNOWN')
                contracts = float(pos.get('contracts', 0))
                notional = float(pos.get('notional', 0))
                leverage = float(pos.get('leverage', 1))
                entry_price = float(pos.get('entryPrice', 0))
                mark_price = float(pos.get('markPrice', 0))
                unrealized_pnl = float(pos.get('unrealizedPnl', 0))
                
                # Calculate margin used for this position
                position_margin = notional / leverage if leverage > 0 else notional
                
                print(f"\n  Symbol:           {symbol}")
                print(f"  Side:             {side.upper()}")
                print(f"  Contracts:        {format_number(contracts, 4)}")
                print(f"  Entry Price:      ${format_number(entry_price)}")
                print(f"  Mark Price:       ${format_number(mark_price)}")
                print(f"  Notional Value:   ${format_number(notional)}")
                print(f"  Leverage:         {leverage}x")
                print(f"  Margin Used:      ${format_number(position_margin)}")
                print(f"  Unrealized P&L:   ${format_number(unrealized_pnl)}")
    
    except Exception as e:
        print(f"❌ Failed to fetch positions: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    print()
    
    # ========================================
    # 5. CALCULATE MARGIN REQUIREMENTS FOR GRID ORDERS
    # ========================================
    print("[5/5] Calculating margin requirements for configured bots...")
    print()
    
    # Get current prices for configured symbols
    symbols_needed = set()
    for bot_name, config in bots_config.items():
        pair = config.get('pair', '')
        if pair:
            symbols_needed.add(pair)
    
    current_prices = {}
    for symbol in symbols_needed:
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_prices[symbol] = float(ticker.get('last', 0))
        except:
            current_prices[symbol] = 0
    
    print("  🧮 MARGIN REQUIREMENT ANALYSIS")
    print("  " + "-"*76)
    
    total_required_margin = 0
    
    for bot_name, config in bots_config.items():
        pair = config.get('pair', '')
        params = config.get('params', {})
        leverage = params.get('leverage', 1)
        
        # Estimate grid order size (this is simplified - real calculation is more complex)
        # Using MAX_ORDER_USD from env or config
        max_order_usd = float(os.getenv('MAX_ORDER_USD', 10000))
        
        # Assume 5 grid levels (default)
        num_grid_levels = 5
        order_size_per_level = max_order_usd / num_grid_levels
        
        # Margin per order = Order Size / Leverage
        margin_per_order = order_size_per_level / leverage if leverage > 0 else order_size_per_level
        total_margin_for_bot = margin_per_order * num_grid_levels
        
        total_required_margin += total_margin_for_bot
        
        current_price = current_prices.get(pair, 0)
        
        print(f"\n  Bot: {bot_name}")
        print(f"  Symbol:                  {pair}")
        print(f"  Current Price:           ${format_number(current_price)}")
        print(f"  Configured Leverage:     {leverage}x")
        print(f"  Est. Grid Levels:        {num_grid_levels}")
        print(f"  Order Size per Level:    ${format_number(order_size_per_level)}")
        print(f"  Margin per Order:        ${format_number(margin_per_order)}")
        print(f"  Total Margin Required:   ${format_number(total_margin_for_bot)}")
    
    print()
    print("  " + "="*76)
    print(f"  TOTAL MARGIN REQUIRED FOR ALL BOTS: ${format_number(total_required_margin)}")
    print(f"  AVAILABLE BALANCE:                   ${format_number(available_balance)}")
    print("  " + "="*76)
    
    # ========================================
    # FINAL DIAGNOSIS
    # ========================================
    print()
    print("="*80)
    print("DIAGNOSIS")
    print("="*80)
    
    if available_balance >= total_required_margin:
        margin_surplus = available_balance - total_required_margin
        print(f"✅ SUFFICIENT MARGIN: You have ${format_number(margin_surplus)} surplus")
        print(f"   Available: ${format_number(available_balance)}")
        print(f"   Required:  ${format_number(total_required_margin)}")
    else:
        margin_deficit = total_required_margin - available_balance
        print(f"❌ INSUFFICIENT MARGIN: You need ${format_number(margin_deficit)} more")
        print(f"   Available: ${format_number(available_balance)}")
        print(f"   Required:  ${format_number(total_required_margin)}")
        print()
        print("   POSSIBLE CAUSES:")
        print("   1. Leverage is set too LOW (check bot configs)")
        print("   2. Account balance is insufficient")
        print("   3. Existing positions are using margin")
    
    print()
    
    # Check if leverage is correctly configured
    leverage_issues = []
    for bot_name, config in bots_config.items():
        params = config.get('params', {})
        leverage = params.get('leverage', 1)
        if leverage < 20:
            leverage_issues.append((bot_name, leverage))
    
    if leverage_issues:
        print("⚠️  LEVERAGE CONFIGURATION ISSUES:")
        for bot_name, lev in leverage_issues:
            print(f"   - {bot_name}: Leverage is {lev}x (Expected: 20x)")
        print()
        print("   ACTION REQUIRED: Update bot leverage settings to 20x")
    else:
        print("✅ All bots have correct leverage settings (20x)")
    
    print()
    print("="*80)
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
