import sys
sys.path.append('.')
from engine.exchange_interface import get_exchange_instance

try:
    ex = get_exchange_instance('future')
    p = ex.get_symbol_precision('SUI/USDC:USDC')
    print("SUI Precision:", p)
except Exception as e:
    print(e)
