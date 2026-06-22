import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    VERSION = "4.1.1"  # v4.1.1: Phase B Exchange-Authoritative Position Sync (Observation-Only)
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

        # 🛡️ SAFETY TOGGLE: Block autonomous execution in production (requires human approval)
        self.REQUIRE_HUMAN_APPROVAL = os.getenv("REQUIRE_HUMAN_APPROVAL", "False").lower() == "true"
        
        # 🛡️ SAFETY TOGGLE: Auto-detect and repair global position wipe (e.g. testnet reset)
        self.ENABLE_GLOBAL_WIPE_DETECTION = os.getenv("ENABLE_GLOBAL_WIPE_DETECTION", "True").lower() == "true"

        # Pair parity: max |virtual - exchange| qty before blocking trade / cycle reset
        self.PAIR_PARITY_QTY_TOLERANCE = float(os.getenv("PAIR_PARITY_QTY_TOLERANCE", "0.002"))
        # Forensic/anonymous WS adopt — off by default (proof-only ledger)
        self.ALLOW_FORENSIC_ADOPT = os.getenv("ALLOW_FORENSIC_ADOPT", "False").lower() == "true"
        # Testnet: when exchange net is 0 but ledger is not, safe-wipe bots (no market order)
        _purge_default = "True" if self.TESTNET else "False"
        self.TESTNET_PURGE_PHANTOM_LEDGER = os.getenv(
            "TESTNET_PURGE_PHANTOM_LEDGER", _purge_default
        ).lower() == "true"
        # When ledger is flat but exchange still holds size, auto repair (adopt with proof or flatten)
        _orphan_default = "False"
        self.AUTO_REPAIR_ORPHAN_EXCHANGE = os.getenv(
            "AUTO_REPAIR_ORPHAN_EXCHANGE", _orphan_default
        ).lower() == "true"
        # One-way: block opposite-direction entry while siblings hold open_qty
        self.ONE_WAY_BLOCK_OPPOSITE_ENTRY = os.getenv(
            "ONE_WAY_BLOCK_OPPOSITE_ENTRY", "True"
        ).lower() == "true"
        # Circuit Breaker: Max quantity allowed to be adopted/aligned for a single bot per cycle (default 0.5)
        self.MAX_ADOPTION_QTY_PER_CYCLE = float(os.getenv("MAX_ADOPTION_QTY_PER_CYCLE", "0.5"))
        # Max quantity allowed to be automatically trimmed/aligned by OWAY_REPAIR (default 50.0)
        self.MAX_OWAY_REPAIR_QTY = float(os.getenv("MAX_OWAY_REPAIR_QTY", "50.0"))

        self.ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.PATHS = {
            "PID_FILE": os.path.join(self.ROOT_DIR, "engine.pid"),
            "STOP_FILE": os.path.join(self.ROOT_DIR, "engine.stop"),
            "EMERGENCY_FILE": os.path.join(self.ROOT_DIR, "engine.emergency"),
            "LOG_FILE": os.path.join(self.ROOT_DIR, "engine.log"),
            "DB_FILE": os.path.join(self.ROOT_DIR, "crypto_bot.db"),
        }

config = Config()
