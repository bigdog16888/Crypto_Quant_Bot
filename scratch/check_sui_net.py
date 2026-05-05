from engine.database import get_pair_virtual_net

def check():
    net = get_pair_virtual_net("SUIUSDC")
    print(f"System Net SUIUSDC: {net}")

if __name__ == "__main__":
    check()
