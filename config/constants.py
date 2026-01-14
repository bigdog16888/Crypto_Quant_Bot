# =========================================
# Crypto Quant Bot - Constants
# Centralized magic numbers and configuration
# =========================================

# ========== ORDER LIMITS ==========
MIN_ORDER_USD = 5.0  # Minimum order size in USD (Binance futures min)
MAX_ORDERS_PER_CYCLE = 10  # Hard cap per polling cycle
MAX_ORDERS_PER_BOT_DAILY = 100  # Maximum orders per bot per day

# ========== TIMING ==========
POLL_INTERVAL_SECONDS = 10  # Main loop polling interval
ORDER_FILL_TIMEOUT_SECONDS = 30  # Wait time for order fill confirmation
MAX_CONSECUTIVE_FAILURES = 5  # Shutdown after this many consecutive cycle failures

# ========== FEES & SLIPPAGE ==========
DEFAULT_FEE_RATE = 0.001  # 0.1% trading fee
DEFAULT_SLIPPAGE_RATE = 0.0005  # 0.05% slippage estimate

# ========== SAFETY ==========
GLOBAL_STOP_LOSS_PCT_DEFAULT = 50.0  # Default circuit breaker threshold

# ========== SUPPORTED CURRENCIES ==========
STABLECOINS = ['USDT', 'USDC']

# ========== UI DEFAULTS ==========
DEFAULT_BASE_SIZE = 10.0
DEFAULT_MARTINGALE_MULTIPLIER = 1.5
DEFAULT_TAKE_PROFIT_USD = 10.0
DEFAULT_TAKE_PROFIT_PCT = 1.0
DEFAULT_MAX_STEPS = 10

# ========== INDICATORS ==========
DEFAULT_ATR_PERIOD = 14
DEFAULT_CCI_PERIOD = 14
DEFAULT_RSI_PERIOD = 14
DEFAULT_BOLL_PERIOD = 20
DEFAULT_BOLL_DEVIATION = 2.0

# ========== TIMEFRAMES ==========
AVAILABLE_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
DEFAULT_TIMEFRAME = "1h"
