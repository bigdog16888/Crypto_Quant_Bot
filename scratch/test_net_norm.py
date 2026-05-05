from engine.database import get_pair_virtual_net
import logging

logging.basicConfig(level=logging.INFO)

print(f"Net for 'BTC/USDC:USDC': {get_pair_virtual_net('BTC/USDC:USDC')}")
print(f"Net for 'BTCUSDC': {get_pair_virtual_net('BTCUSDC')}")
print(f"Net for 'LINKUSDC': {get_pair_virtual_net('LINKUSDC')}")
