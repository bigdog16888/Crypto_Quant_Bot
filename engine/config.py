import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    API_KEY = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_API_SECRET")
    
    DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT").split(",")
    MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", 100))

config = Config()
