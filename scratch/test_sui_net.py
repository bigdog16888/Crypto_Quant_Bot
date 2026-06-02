import sys
import os
sys.path.append(os.getcwd())

from engine.database import get_pair_virtual_net
from engine.exchange_interface import normalize_symbol

def run():
    print("get_pair_virtual_net('SUI/USDC:USDC'):", get_pair_virtual_net('SUI/USDC:USDC'))
    print("get_pair_virtual_net('SUIUSDC'):", get_pair_virtual_net('SUIUSDC'))
    print("normalize_symbol('SUI/USDC:USDC'):", normalize_symbol('SUI/USDC:USDC'))
    print("normalize_symbol('SUIUSDC'):", normalize_symbol('SUIUSDC'))

if __name__ == '__main__':
    run()
