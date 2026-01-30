
# Debug the UI logic for Safe Min Size
import sys
import os

# Mock Streamlit caching if needed or just import direct
from engine.exchange_interface import ExchangeInterface

def get_exchange_instance(market_type):
    return ExchangeInterface(market_type=market_type, validate=False)

def fetch_last_price(exchange, symbol):
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker['last']) if ticker else 0.0

print("--- DEBUG UI LOGIC ---")
pair = "BTC/USDC"
market_type = "future"
print(f"Pair: {pair}, Type: {market_type}")

try:
    exchange = get_exchange_instance(market_type)
    print("Exchange initialized")
    
    price = fetch_last_price(exchange, pair)
    print(f"Price: ${price}")
    
    if price > 0:
        safe_min = exchange.calculate_safe_min_size(pair, price)
        print(f"Calculated Safe Min: ${safe_min}")
    else:
        print("Price is 0, skipping calc")
        
except Exception as e:
    print(f"Error: {e}")
