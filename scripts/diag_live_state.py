#!/usr/bin/env python3
"""Read-only parity diagnostic: virtual net vs exchange per pair."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.database import get_pair_virtual_net, get_connection, audit_pair_ledger_vs_exchange
from engine.exchange_interface import ExchangeInterface

def main():
    conn = get_connection()
    ex = ExchangeInterface('future')
    print('=== parity audit ===')
    for row in audit_pair_ledger_vs_exchange(ex):
        print(' ', row)
    print('\n=== virtual net (key pairs) ===')
    for sym in sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT pair FROM bots WHERE is_active=1"
    ).fetchall()}):
        print(f'  {sym}: {get_pair_virtual_net(sym):.4f}')
    print('\n=== bots in trade ===')
    for row in conn.execute(
        """
        SELECT b.id, b.name, b.pair, b.direction, b.status,
               t.total_invested, t.open_qty, t.current_step
        FROM bots b JOIN trades t ON t.bot_id=b.id
        WHERE b.is_active=1 AND t.total_invested > 0.01
        ORDER BY t.total_invested DESC
        """
    ):
        print(' ', row)

if __name__ == '__main__':
    main()
