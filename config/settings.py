import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    VERSION = "3.0.3"  # Hedge-Aware Reconciliation + Unpacking Fix
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def __init__(self):
        self.TESTNET = os.getenv("TESTNET", "True").lower() == "true"
        self.FUTURES_ONLY_MODE = os.getenv("FUTURES_ONLY_MODE", "True" if self.TESTNET else "False").lower() == "true"
        
        if self.TESTNET:
            self.API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", os.getenv("BINANCE_API_KEY", ""))
            self.API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", os.getenv("BINANCE_API_SECRET", ""))
        else:
            self.API_KEY = os.getenv("BINANCE_API_KEY", "")
            self.API_SECRET = os.getenv("BINANCE_API_SECRET", "")

        self.DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
        self.TRADING_ENABLED = os.getenv("TRADING_ENABLED", "True").lower() == "true"
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        self.EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance").lower()
        self.DEMO_TRADING = os.getenv("DEMO_TRADING", "True").lower() == "true"
        self.MARKET_TYPE = os.getenv("MARKET_TYPE", "future").lower()
        self.ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,BTC/USDC,ETH/USDC,SOL/USDC").split(",")
        
        self.MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", 10000))
        
        # ATR Configuration for UI/Strategy
        self.ATR_TIMEFRAME = os.getenv("ATR_TIMEFRAME", "1h")
        self.ATR_PERIODS = int(os.getenv("ATR_PERIODS", 14))

        self.METRICS_PORT = int(os.getenv("METRICS_PORT", 9090))

        self.GLOBAL_STOP_LOSS_PCT = float(os.getenv("GLOBAL_STOP_LOSS_PCT", 50.0))
        self.MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
        self.RETRY_DELAY = int(os.getenv("RETRY_DELAY", 2))
        
        # 🛡️ SAFETY TOGGLE: Allow user to disable auto-cancellation of zombie orders
        self.AUTO_FIX_ZOMBIES = os.getenv("AUTO_FIX_ZOMBIES", "True").lower() == "true"
        
        # 🛡️ SAFETY LIMIT: Maximum account drawdown percentage before blocking new entries (Default 80%)
        self.MAX_ACCOUNT_DRAWDOWN_PERCENT = float(os.getenv("MAX_ACCOUNT_DRAWDOWN_PERCENT", 80.0))
        
        # 🛡️ SAFETY TOGGLE: Strict Cleanup (True = Kill Manual Orders, False = Protect Them)
        self.STRICT_CLEANUP = os.getenv("STRICT_CLEANUP", "False").lower() == "true"

        self.ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.PATHS = {
            "PID_FILE": os.path.join(self.ROOT_DIR, "engine.pid"),
            "STOP_FILE": os.path.join(self.ROOT_DIR, "engine.stop"),
            "EMERGENCY_FILE": os.path.join(self.ROOT_DIR, "engine.emergency"),
            "LOG_FILE": os.path.join(self.ROOT_DIR, "engine.log"),
            "DB_FILE": os.path.join(self.ROOT_DIR, "crypto_bot.db"),
        }

config = Config()
