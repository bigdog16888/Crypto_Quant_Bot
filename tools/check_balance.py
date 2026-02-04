#!/usr/bin/env python3
"""Check testnet balance and positions to debug insufficient balance errors."""

import sys
sys.path.insert(0, '.')

from config.settings import config
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 60)
    print("TESTNET BALANCE & POSITION CHECK")
    print("=" * 60)
    
    # Initialize exchange same way as bot
    ex = ExchangeInterface(market_type='future')
    
    # Fetch balance
    print("\n📊 BALANCE:")
    try:
        balance = ex.fetch_balance()
        
        # Check USDT
        usdt = balance.get('USDT', {})
        print(f"   USDT: Total={usdt.get('total', 0):.2f}, Free={usdt.get('free', 0):.2f}, Used={usdt.get('used', 0):.2f}")
        
        # Check USDC
        usdc = balance.get('USDC', {})
        print(f"   USDC: Total={usdc.get('total', 0):.2f}, Free={usdc.get('free', 0):.2f}, Used={usdc.get('used', 0):.2f}")
        
    except Exception as e:
        print(f"   ❌ Error fetching balance: {e}")
    
    # Fetch positions
    print("\n📈 OPEN POSITIONS:")
    try:
        positions = ex.fetch_positions()
        open_positions = [p for p in positions if float(p.get('contracts', 0) or 0) != 0]
        
        if open_positions:
            for p in open_positions:
                symbol = p.get('symbol')
                side = p.get('side')
                contracts = p.get('contracts', 0)
                notional = p.get('notional', 0)
                pnl = p.get('unrealizedPnl', 0)
                margin = p.get('initialMargin', 0)
                print(f"   {symbol}: {side} {contracts} (Notional: ${notional:.2f}, Margin: ${margin:.2f}, uPnL: ${pnl:.2f})")
        else:
            print("   No open positions.")
    except Exception as e:
        print(f"   ❌ Error fetching positions: {e}")
    
    # Fetch open orders
    print("\n📋 OPEN ORDERS:")
    try:
        orders = ex.exchange.fetch_open_orders()
        if orders:
            for o in orders:
                print(f"   {o['symbol']} {o['side']} {o['amount']} @ {o['price']} ({o.get('clientOrderId', 'N/A')[:20]})")
        else:
            print("   No open orders.")
    except Exception as e:
        print(f"   ❌ Error fetching orders: {e}")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
