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
        "EEStartLevel": 1     # Minimum grid level to activate EE
    }

