"""Check exchange state to debug insufficient balance errors"""
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')

# Get balance
balance = ex.exchange.fetch_balance()
print("=== BALANCE ===")
print(f"USDC Free: {balance['USDC']['free']}")
print(f"USDC Used: {balance['USDC']['used']}")
print(f"USDC Total: {balance['USDC']['total']}")

# Get positions
print("\n=== OPEN POSITIONS ===")
positions = ex.exchange.fetch_positions()
open_count = 0
for p in positions:
    contracts = float(p.get('contracts', 0))
    if contracts != 0:
        open_count += 1
        symbol = p.get('symbol', 'N/A')
        side = p.get('side', 'N/A')
        entry = p.get('entryPrice', 'N/A')
        margin = p.get('initialMargin', 'N/A')
        print(f"  {symbol}: {side} {contracts} contracts, Entry: {entry}, Margin: {margin}")
if open_count == 0:
    print("  No open positions")

# Get open orders
print("\n=== OPEN ORDERS ===")
orders = ex.exchange.fetch_open_orders()
print(f"Total: {len(orders)}")
for o in orders:
    symbol = o.get('symbol', 'N/A')
    side = o.get('side', 'N/A')
    otype = o.get('type', 'N/A')
    amount = o.get('amount', 'N/A')
    price = o.get('price', 'market')
    print(f"  {symbol}: {side} {otype} {amount} @ {price}")

# Check what bots are trying to do
print("\n=== BOT DATABASE STATE ===")
from engine.database import get_all_bots, get_connection
conn = get_connection()
cur = conn.cursor()
bots = cur.execute("SELECT id, name, pair, direction FROM bots WHERE is_active=1").fetchall()
print(f"Active bots: {len(bots)}")
for b in bots:
    bot_id, name, pair, direction = b
    trade = cur.execute("SELECT current_step, total_invested FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    step = trade[0] if trade else 0
    invested = trade[1] if trade else 0.0
    print(f"  Bot {bot_id} ({name}): {pair} {direction} | Step: {step} | Invested: ${invested:.2f}")
conn.close()

# Check leverage
print("\n=== LEVERAGE CHECK ===")
try:
    leverage_info = ex.exchange.fapiPrivateGetPositionSideDual()
    print(f"Position Mode (Dual): {leverage_info}")
except Exception as e:
    print(f"Could not get position mode: {e}")

# Try to get BTC leverage specifically
try:
    lev = ex.exchange.fapiPrivateGetLeverageBracket({"symbol": "BTCUSDC"})
    print(f"BTC/USDC Leverage Brackets: {lev}")
except Exception as e:
    print(f"Could not get leverage: {e}")
