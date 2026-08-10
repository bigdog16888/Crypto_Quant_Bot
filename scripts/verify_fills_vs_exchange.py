#!/usr/bin/env python3
"""
Verify all fills for bots 10016 and 100317 cycle 3 against Binance trade history.
Computes BUY total, SELL total, net programmatically.
"""

import sys
sys.path.insert(0, r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')

from engine import database
from engine.exchange_interface import ExchangeInterface

database.DB_PATH = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
database.close_connection()
conn = database.get_connection()
ex = ExchangeInterface()

# Get all Binance trades for BTC/USDC
print("Fetching Binance trade history...")
trades = ex.fetch_my_trades('BTC/USDC:USDC', limit=500)

# Build lookup: exchange_order_id -> list of trades
exchange_trades_by_order = {}
for t in trades:
    oid = t.get('order')
    if oid:
        exchange_trades_by_order.setdefault(str(oid), []).append(t)

# Get all fills for 10016 cycle 3
rows_10016 = conn.execute('''
    SELECT order_id, client_order_id, order_type, status, filled_amount, price, position_side, step, created_at
    FROM bot_orders
    WHERE bot_id = 10016 AND cycle_id = 3 AND filled_amount > 0
    ORDER BY created_at
''').fetchall()

# Get all fills for 100317 cycle 3
rows_100317 = conn.execute('''
    SELECT order_id, client_order_id, order_type, status, filled_amount, price, position_side, step, created_at
    FROM bot_orders
    WHERE bot_id = 100317 AND cycle_id = 3 AND filled_amount > 0
    ORDER BY created_at
''').fetchall()

print("\n" + "="*80)
print("BOT 10016 CYCLE 3 - EXCHANGE VERIFICATION")
print("="*80)

buy_total_10016 = 0.0
sell_total_10016 = 0.0
phantom_10016 = 0.0

for r in rows_10016:
    oid = str(r[0])
    cid = r[1]
    otype = r[2]
    status = r[3]
    qty = float(r[4])
    price = float(r[5])
    pos_side = r[6]
    step = r[7]
    ts = r[8]
    
    # Check exchange
    exchange_fills = exchange_trades_by_order.get(oid, [])
    if exchange_fills:
        for t in exchange_fills:
            side = t.get('side', '')
            amt = float(t.get('amount', 0))
            if side == 'buy':
                buy_total_10016 += amt
                print(f"  VERIFIED BUY:  {oid} {cid} type={otype} amt={amt:.6f} price={price:.2f} step={step}")
            elif side == 'sell':
                sell_total_10016 += amt
                print(f"  VERIFIED SELL: {oid} {cid} type={otype} amt={amt:.6f} price={price:.2f} step={step}")
    else:
        phantom_10016 += qty
        print(f"  PHANTOM:       {oid} {cid} type={otype} qty={qty:.6f} pos={pos_side} step={step} (NO EXCHANGE MATCH)")

print(f"\n  10016 BUY total:  {buy_total_10016:.6f}")
print(f"  10016 SELL total: {sell_total_10016:.6f}")
print(f"  10016 NET:        {buy_total_10016 - sell_total_10016:.6f}")
print(f"  10016 PHANTOM:    {phantom_10016:.6f}")

print("\n" + "="*80)
print("BOT 100317 CYCLE 3 - EXCHANGE VERIFICATION")
print("="*80)

buy_total_100317 = 0.0
sell_total_100317 = 0.0
phantom_100317 = 0.0

for r in rows_100317:
    oid = str(r[0])
    cid = r[1]
    otype = r[2]
    status = r[3]
    qty = float(r[4])
    price = float(r[5])
    pos_side = r[6]
    step = r[7]
    ts = r[8]
    
    exchange_fills = exchange_trades_by_order.get(oid, [])
    if exchange_fills:
        for t in exchange_fills:
            side = t.get('side', '')
            amt = float(t.get('amount', 0))
            if side == 'buy':
                buy_total_100317 += amt
                print(f"  VERIFIED BUY:  {oid} {cid} type={otype} amt={amt:.6f} price={price:.2f} step={step}")
            elif side == 'sell':
                sell_total_100317 += amt
                print(f"  VERIFIED SELL: {oid} {cid} type={otype} amt={amt:.6f} price={price:.2f} step={step}")
    else:
        phantom_100317 += qty
        print(f"  PHANTOM:       {oid} {cid} type={otype} qty={qty:.6f} pos={pos_side} step={step} (NO EXCHANGE MATCH)")

print(f"\n  100317 BUY total:  {buy_total_100317:.6f}")
print(f"  100317 SELL total: {sell_total_100317:.6f}")
print(f"  100317 NET:        {buy_total_100317 - sell_total_100317:.6f}")
print(f"  100317 PHANTOM:    {phantom_100317:.6f}")

print("\n" + "="*80)
print("COMBINED NET (One-Way Mode: single BTCUSDC position)")
print("="*80)

total_buy = buy_total_10016 + buy_total_100317
total_sell = sell_total_10016 + sell_total_100317
combined_net = total_buy - total_sell

print(f"  TOTAL BUY:  {total_buy:.6f}")
print(f"  TOTAL SELL: {total_sell:.6f}")
print(f"  NET:        {combined_net:.6f}")
print(f"  PHANTOM TOTAL: {phantom_10016 + phantom_100317:.6f}")

# Also fetch current exchange position
print("\n" + "="*80)
print("CURRENT EXCHANGE POSITION")
print("="*80)
positions = ex.fetch_positions()
for p in positions:
    if 'BTC' in str(p.get('symbol', '')):
        print(f"  {p}")

database.close_connection()