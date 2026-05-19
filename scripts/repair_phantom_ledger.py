#!/usr/bin/env python3
"""One-shot: purge phantom ledger rows when exchange net is 0 (testnet-safe)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.exchange_interface import ExchangeInterface
from engine.parity_gates import startup_repair_mismatched_pairs
from engine.database import audit_pair_ledger_vs_exchange, get_pair_virtual_net

def main():
    ex = ExchangeInterface(market_type='future')
    print('Before:')
    for sym in ['XRP/USDC:USDC', 'SUI/USDC:USDC']:
        print(f'  {sym} virtual={get_pair_virtual_net(sym):.4f}')
    for row in audit_pair_ledger_vs_exchange(ex):
        print(f'  mismatch {row}')

    summary = startup_repair_mismatched_pairs(ex)
    print('\nRepair:', summary)

    print('\nAfter:')
    for sym in ['XRP/USDC:USDC', 'SUI/USDC:USDC']:
        print(f'  {sym} virtual={get_pair_virtual_net(sym):.4f}')
    for row in audit_pair_ledger_vs_exchange(ex):
        print(f'  mismatch {row}')

if __name__ == '__main__':
    main()
