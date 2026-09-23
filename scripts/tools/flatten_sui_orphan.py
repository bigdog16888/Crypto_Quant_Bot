#!/usr/bin/env python3
"""
Flatten SUI/USDC orphan position on Binance Futures Testnet.

Usage:
    python scripts/flatten_sui_orphan.py --dry-run    # Show order payload only (default)
    python scripts/flatten_sui_orphan.py --execute    # Actually place the order
"""

import os
import sys
import argparse

# Load project config
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config.settings import config
from engine.exchange_interface import ExchangeInterface

SYMBOL = "SUI/USDC:USDC"


def fetch_position(exchange, symbol):
    """Fetch current position for symbol."""
    positions = exchange.fetch_positions()
    # Normalize symbol for comparison: SUI/USDC:USDC -> SUI/USDC
    target = symbol.split(':')[0] if ':' in symbol else symbol
    for pos in positions:
        pos_sym = pos['symbol'].split(':')[0] if ':' in pos['symbol'] else pos['symbol']
        if pos_sym == target:
            contracts = float(pos.get('contracts', 0) or 0)
            if contracts != 0:
                return {
                    'symbol': pos['symbol'],
                    'contracts': contracts,
                    'side': pos.get('side'),
                    'entryPrice': pos.get('entryPrice'),
                }
    return None


def main():
    parser = argparse.ArgumentParser(description="Flatten SUI/USDC orphan on Binance Testnet")
    parser.add_argument('--execute', action='store_true', help='Actually place the order (default: dry-run)')
    parser.add_argument('--dry-run', action='store_true', help='Show order payload only (default)')
    args = parser.parse_args()

    # Default to dry-run unless --execute explicitly passed
    dry_run = not args.execute

    print(f"{'='*60}")
    print(f"SUI/USDC ORPHAN FLATTENING - {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    print(f"{'='*60}")

    exchange = ExchangeInterface(market_type='future')

    # Fetch current position
    position = fetch_position(exchange, SYMBOL)

    if not position:
        print(f"✅ No open position found for {SYMBOL}. Nothing to do.")
        return 0

    contracts = position['contracts']
    side = position['side']

    print(f"📊 Current position: {contracts} {side} (entry: {position['entryPrice']})")

    # Determine flatten order side (opposite of position)
    # Long position -> SELL to flatten; Short position -> BUY to flatten
    if side.lower() == 'long':
        order_side = 'sell'
    elif side.lower() == 'short':
        order_side = 'buy'
    else:
        print(f"❌ Unknown position side: {side}")
        return 1

    # Prepare order parameters
    order_params = {
        'symbol': SYMBOL,
        'type': 'market',
        'side': order_side,
        'amount': abs(contracts),
        'params': {
            'reduceOnly': True,
        }
    }

    print(f"\n📋 Order payload:")
    print(f"   Symbol: {order_params['symbol']}")
    print(f"   Type: {order_params['type']}")
    print(f"   Side: {order_params['side'].upper()}")
    print(f"   Amount: {order_params['amount']}")
    print(f"   Params: {order_params['params']}")

    if dry_run:
        print(f"\n🔍 DRY RUN - Order NOT sent. Use --execute to place order.")
        return 0

    # Execute the order
    print(f"\n🚀 Placing market {order_side.upper()} order for {abs(contracts)} contracts...")
    try:
        import time
        cid = f"CQB_MANUAL_FLATTEN_SUI_{int(time.time())}"
        order = exchange.create_order(
            symbol=SYMBOL,
            type="market",
            side=order_side,
            amount=abs(contracts),
            params={'reduceOnly': True, 'newClientOrderId': cid, 'human_approved': True},
            _call_site="scripts/flatten_sui_orphan.py",
        )
        print(f"✅ Order placed successfully!")
        print(f"   Order ID: {order.get('id')}")
        print(f"   Status: {order.get('status')}")
        print(f"   Filled: {order.get('filled', 0)} / {order.get('amount', 0)}")
        return 0
    except Exception as e:
        print(f"❌ Order failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())