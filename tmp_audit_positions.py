
import logging
import sys
import os

# Set up logging to avoid cluttered output
logging.basicConfig(level=logging.ERROR)

# Add parent dir to path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def audit_real_positions():
    print(f"--- Exchange Audit (Mode: {'TESTNET' if config.TESTNET else 'MAINNET'}) ---")
    try:
        ex = ExchangeInterface('future')
        positions = ex.fetch_positions()
        
        if positions is None:
            print("❌ Failed to fetch positions (Auth/Network Error).")
            return

        active = [p for p in positions if abs(p['contracts']) > 0]
        if not active:
            print("✅ Exchange is FLAT (0 positions).")
        else:
            print(f"⚠️ Exchange has {len(active)} active positions:")
            for p in active:
                notional = p['contracts'] * p['entryPrice']
                print(f" - {p['symbol']}: {p['contracts']} contracts @ {p['entryPrice']} (Notional: ${abs(notional):.2f})")

        # Check balance too
        balance = ex.fetch_balance()
        if balance and 'total' in balance:
            print(f"\nBalance: {balance['total']}")

    except Exception as e:
        print(f"❌ Error during audit: {e}")

if __name__ == "__main__":
    audit_real_positions()
