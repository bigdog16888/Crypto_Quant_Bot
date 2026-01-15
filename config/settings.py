import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # --- Path Configuration (Universal) ---
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    PATHS = {
        "PID_FILE": os.path.join(ROOT_DIR, "engine.pid"),
        "STOP_FILE": os.path.join(ROOT_DIR, "engine.stop"),
        "EMERGENCY_FILE": os.path.join(ROOT_DIR, "engine.emergency"),
        "LOG_FILE": os.path.join(ROOT_DIR, "engine.log"),
        "DB_FILE": os.path.join(ROOT_DIR, "crypto_bot.db"),
    }

    # Use testnet keys when TESTNET is True
    TESTNET = os.getenv("TESTNET", "True").lower() == "true"
    if TESTNET:
        API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", os.getenv("BINANCE_API_KEY", ""))
        API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", os.getenv("BINANCE_API_SECRET", ""))
    else:
        API_KEY = os.getenv("BINANCE_API_KEY", "")
        API_SECRET = os.getenv("BINANCE_API_SECRET", "")

    DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Support both SPOT (for USDC pairs) and FUTURES (for USDT pairs)
    # Users select via UI dropdown in Bot Creator/Manager
    ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,BTC/USDC,ETH/USDC,SOL/USDC").split(",")
    MARKET_TYPE = os.getenv("MARKET_TYPE", "future").lower() # 'spot' or 'future' (USDT-M) or 'swap' - DEFAULT: FUTURES
    MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", 100))
    
    # Circuit Breaker / Safety
    GLOBAL_STOP_LOSS_PCT = float(os.getenv("GLOBAL_STOP_LOSS_PCT", 50.0)) # Stop if account drops 50%
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
    RETRY_DELAY = int(os.getenv("RETRY_DELAY", 2))

config = Config()

def get_settings():
    """
    Returns a dictionary of global and default strategy settings.
    In a real app, this might fetch from a database or JSON file.
    """
    return {
        # Grid Settings
        "UseATRGrid": True,
        "ATRGridFactor": 1.0,  # Multiplier for ATR
        "GridLevelMultipliers": {
            1: 1.0,
            2: 1.0,
            3: 1.2,
            4: 1.5,
            5: 2.0
            # ... and so on
        },
        
        # Early Exit Settings
        "UseEarlyExit": True,
        "EEHoursPC": 0.5,     # Percent decay per hour
        "EEStartHours": 2.0,  # Start decay after X hours
        "EEStartLevel": 1,     # Minimum grid level to activate EE
        
        # Market Maker Settings (Defaults)
        "SpreadPct": 0.002,         # 0.2%
        "SkewFactor": 0.0,          # Neutral
        "MMOrderSize": 0.01,
        "MaxInventory": 1.0,
        "RepriceThreshold": 0.001   # 0.1%
    }
