from engine.database import get_pair_virtual_net, get_connection
import logging

logging.basicConfig(level=logging.INFO)

pair = 'SUIUSDC'
net = get_pair_virtual_net(pair)
print(f"\nFinal get_pair_virtual_net('{pair}') result: {net}")
