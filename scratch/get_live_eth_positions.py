import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

class UserExchangeWrapper:
    def __init__(self):
        self.ex = ExchangeInterface()

    def fetch_positions(self, symbols=None):
        positions = self.ex.fetch_positions()
        if not positions:
            return []
        if symbols:
            target_symbols = []
            for sym in symbols:
                target_symbols.append(sym.upper())
                if ':' in sym:
                    target_symbols.append(sym.split(':')[0].upper())
                target_symbols.append(sym.replace('/', '').replace(':', '').upper())
            
            filtered = []
            for p in positions:
                p_sym = p.get('symbol', '').upper()
                p_norm = p_sym.replace('/', '').replace(':', '').upper()
                if p_sym in target_symbols or p_norm in target_symbols:
                    filtered.append(p)
            return filtered
        return positions

exchange = UserExchangeWrapper()

# --- USER REQUESTED QUERY ---
positions = exchange.fetch_positions(['ETH/USDC:USDC'])
for p in positions:
    if float(p.get('contracts', 0)) != 0:
        print(p['symbol'], p['side'], p['contracts'], p['entryPrice'])
