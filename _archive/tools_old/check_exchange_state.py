#!/usr/bin/env python3
"""
Exchange State Checker - Verify what's actually on Binance
Created: 2026-02-09 for crash recovery
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 60)
    print("EXCHANGE STATE CHECK")
    print("=" * 60)
    
    try:
        ex = ExchangeInterface()
        
        # 1. Check open positions
        print("\n### 1. OPEN POSITIONS ON EXCHANGE ###")
        try:
            positions = ex.get_open_positions()
            if positions:
                for pos in positions:
                    symbol = pos.get('symbol', 'Unknown')
                    side = pos.get('side', 'Unknown')
                    size = pos.get('contracts', pos.get('amount', 0))
                    entry = pos.get('entryPrice', 0)
                    pnl = pos.get('unrealizedPnl', 0)
                    print(f"  {symbol}: {side} {size} @ {entry} (PnL: ${pnl:.2f})")
            else:
                print("  ⚠️ NO OPEN POSITIONS")
        except Exception as e:
            print(f"  Error getting positions: {e}")
        
        # 2. Check open orders
        print("\n### 2. OPEN ORDERS ON EXCHANGE ###")
        try:
            orders = ex.get_open_orders()
            if orders:
                for order in orders:
                    symbol = order.get('symbol', 'Unknown')
                    side = order.get('side', 'Unknown')
                    otype = order.get('type', 'Unknown')
                    price = order.get('price', 0)
                    amount = order.get('amount', 0)
                    client_id = order.get('clientOrderId', 'N/A')
                    print(f"  {symbol}: {side} {otype} {amount} @ {price} (ID: {client_id})")
            else:
                print("  ⚠️ NO OPEN ORDERS")
        except Exception as e:
            print(f"  Error getting orders: {e}")
        
        # 3. Check balance
        print("\n### 3. ACCOUNT BALANCE ###")
        try:
            balance = ex.exchange.fetch_balance()
            usdc = balance.get('USDC', {})
            usdt = balance.get('USDT', {})
            print(f"  USDC: Free={usdc.get('free', 0):.2f}, Total={usdc.get('total', 0):.2f}")
            print(f"  USDT: Free={usdt.get('free', 0):.2f}, Total={usdt.get('total', 0):.2f}")
        except Exception as e:
            print(f"  Error getting balance: {e}")
            
    except Exception as e:
        print(f"❌ Failed to connect to exchange: {e}")
        print("Make sure .env has correct API keys")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
