import os
from dotenv import load_dotenv
import ccxt

load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

exchange = ccxt.binanceusdm({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})
if os.getenv("TESTNET", "False").lower() == "true":
    exchange.set_sandbox_mode(True)

try:
    positions = exchange.fapiPrivateV2GetPositionRisk()
    for p in positions:
        amt = float(p['positionAmt'])
        if amt != 0:
            print(f"Physical Pos: {p['symbol']}  amt={amt}  entry={p['entryPrice']}")
except Exception as e:
    print(f"Error fetching: {e}")
