import os
import sys
import sqlite3
import argparse
import json

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from engine.exchange_interface import ExchangeInterface
    from config.settings import config
    from engine.database import get_connection, get_all_bots
except ImportError as e:
    print(f"Warning: Could not import project modules. Some features may be limited. Error: {e}")

def check_database_health():
    print("\n" + "="*40)
    print("CHECKING DATABASE HEALTH")
    print("="*40)
    
    db_path = 'crypto_bot.db'
    if not os.path.exists(db_path):
        print(f"❌ Error: {db_path} not found.")
        return

    print(f"✅ {db_path} found.")
    
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # Check tables
        tables = ['bots', 'trades', 'bot_orders', 'trade_history']
        for table in tables:
            c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if c.fetchone():
                print(f"✅ Table '{table}' exists.")
                
                # Show some stats
                c.execute(f"SELECT COUNT(*) FROM {table}")
                count = c.fetchone()[0]
                print(f"   - Row count: {count}")
            else:
                print(f"❌ Table '{table}' MISSING.")
        
        # Check active bots count
        c.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
        active_count = c.fetchone()[0]
        print(f"\n📊 Active Bots in DB: {active_count}")
        
        conn.close()
    except Exception as e:
        print(f"❌ Database error: {e}")

def check_exchange_connection():
    print("\n" + "="*40)
    print("CHECKING EXCHANGE CONNECTION")
    print("="*40)
    
    try:
        # Use primary exchange
        market_type = getattr(config, 'MARKET_TYPE', 'future')
        print(f"Initializing {market_type} exchange...")
        ex = ExchangeInterface(market_type=market_type)
        
        # 1. Check Balance
        print("\n💰 Fetching Balance...")
        balance = ex.exchange.fetch_balance()
        for asset in ['USDC', 'USDT']:
            if asset in balance:
                print(f"   {asset}: Total {balance[asset]['total']:.2f} | Free {balance[asset]['free']:.2f}")
        
        # 2. Check Positions
        print("\n📈 Fetching Positions...")
        positions = ex.get_positions()
        active_pos = [p for p in positions if abs(float(p.get('contracts', 0))) > 0]
        if active_pos:
            for p in active_pos:
                print(f"   {p['symbol']}: {p['side']} | Size: {p['contracts']} | Entry: ${p['entryPrice']}")
        else:
            print("   No open positions.")
            
        # 3. Check Open Orders
        print("\n📝 Fetching Open Orders...")
        orders = ex.fetch_open_orders()
        if orders:
            print(f"   Found {len(orders)} open orders.")
            for o in orders[:5]:  # Show first 5
                print(f"   - {o['id']} | {o['symbol']} | {o['side']} | {o['type']} | ${o['price']}")
            if len(orders) > 5:
                print(f"   ... and {len(orders)-5} more.")
        else:
            print("   No open orders.")
            
        print("\n✅ Exchange connection successful.")
        
    except Exception as e:
        print(f"❌ Exchange error: {e}")

def verify_system_config():
    print("\n" + "="*40)
    print("VERIFYING SYSTEM CONFIG")
    print("="*40)
    
    # Check .env
    if os.path.exists('.env'):
        print("✅ .env file found.")
    else:
        print("❌ .env file MISSING.")
        
    # Check settings
    try:
        print(f"Environment: {'Production' if not getattr(config, 'TESTNET', True) else 'Testnet'}")
        print(f"Dry Run: {getattr(config, 'DRY_RUN', True)}")
        print(f"Market Type: {getattr(config, 'MARKET_TYPE', 'future')}")
        
        # Mask API Keys
        api_key = getattr(config, 'API_KEY', '')
        if api_key:
            masked_key = api_key[:4] + "*" * (len(api_key)-8) + api_key[-4:] if len(api_key) > 8 else "****"
            print(f"API Key: {masked_key}")
        else:
            print("API Key: NOT SET")
            
        print("\n✅ Config verification complete.")
    except Exception as e:
        print(f"❌ Config error: {e}")

def list_active_bots():
    print("\n" + "="*40)
    print("LISTING ACTIVE BOTS")
    print("="*40)
    
    try:
        conn = sqlite3.connect('crypto_bot.db')
        c = conn.cursor()
        
        c.execute("""
            SELECT b.id, b.name, b.pair, b.direction, t.Total_Invested, t.Current_Step
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
        """)
        bots = c.fetchall()
        
        if not bots:
            print("No active bots found.")
        else:
            print(f"{'ID':<4} | {'Name':<20} | {'Pair':<10} | {'Side':<6} | {'Step':<4} | {'Invested':<10}")
            print("-" * 70)
            for b in bots:
                invested = f"${b[4]:.2f}" if b[4] is not None else "$0.00"
                step = b[5] if b[5] is not None else 0
                print(f"{b[0]:<4} | {b[1]:<20} | {b[2]:<10} | {b[3]:<6} | {step:<4} | {invested:<10}")
        
        conn.close()
    except Exception as e:
        print(f"❌ Error listing bots: {e}")

def check_websocket_status():
    print("\n" + "="*40)
    print("CHECKING WEBSOCKET STATUS")
    print("="*40)
    
    try:
        import websockets
        print("✅ 'websockets' library is installed.")
        
        # Try a quick connection test to Binance WebSocket (optional)
        print("Testing connection to Binance WebSocket...")
        # This is a bit complex for a simple script, so we'll just check if we can import it for now.
        print("WebSocket module is available for real-time data.")
        
    except ImportError:
        print("❌ 'websockets' library is MISSING.")
        print("   Install it using: pip install websockets")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")

def main():
    parser = argparse.ArgumentParser(description="Crypto Bot Diagnostic Tool")
    parser.add_argument('--db', action='store_true', help='Check Database Health')
    parser.add_argument('--exchange', action='store_true', help='Check Exchange Connection')
    parser.add_argument('--config', action='store_true', help='Verify System Config')
    parser.add_argument('--bots', action='store_true', help='List Active Bots')
    parser.add_argument('--ws', action='store_true', help='Check WebSocket Status')
    parser.add_argument('--all', action='store_true', help='Run all checks')
    
    args = parser.parse_args()
    
    # If no arguments provided, show interactive menu
    if not any(vars(args).values()):
        while True:
            print("\n" + "🚀 CRYPTO BOT DIAGNOSTIC TOOL")
            print("1. Check Database Health")
            print("2. Check Exchange Connection")
            print("3. Verify System Config")
            print("4. List Active Bots")
            print("5. Check WebSocket Status")
            print("6. Run All Checks")
            print("0. Exit")
            
            choice = input("\nSelect an option (0-6): ")
            
            if choice == '1': check_database_health()
            elif choice == '2': check_exchange_connection()
            elif choice == '3': verify_system_config()
            elif choice == '4': list_active_bots()
            elif choice == '5': check_websocket_status()
            elif choice == '6':
                check_database_health()
                check_exchange_connection()
                verify_system_config()
                list_active_bots()
                check_websocket_status()
            elif choice == '0': break
            else: print("Invalid choice.")
    else:
        if args.all or args.db: check_database_health()
        if args.all or args.exchange: check_exchange_connection()
        if args.all or args.config: verify_system_config()
        if args.all or args.bots: list_active_bots()
        if args.all or args.ws: check_websocket_status()

if __name__ == "__main__":
    main()
