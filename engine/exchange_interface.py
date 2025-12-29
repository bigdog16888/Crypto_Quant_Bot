import ccxt
import time
import logging
from .config import config

class ExchangeInterface:
    def __init__(self, exchange_id='binance'):
        self.exchange_id = exchange_id
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot'
            }
        })
        self.logger = logging.getLogger(__name__)

    def _safe_request(self, method, *args, **kwargs):
        """Wrapper for API calls with basic error handling and security checks."""
        try:
            # Security check: Whitelist
            if 'symbol' in kwargs and kwargs['symbol'] not in config.ALLOWED_SYMBOLS:
                raise ValueError(f"Symbol {kwargs['symbol']} is not in whitelisted ALLOWED_SYMBOLS")
            
            # Security check: Max order size (if applicable)
            if 'amount' in kwargs and 'price' in kwargs:
                usd_value = float(kwargs['amount']) * float(kwargs['price'])
                if usd_value > config.MAX_ORDER_USD:
                    raise ValueError(f"Order value ${usd_value} exceeds MAX_ORDER_USD safety limit")

            if config.DRY_RUN:
                self.logger.info(f"[DRY_RUN] Would call {method} with {args} {kwargs}")
                return {"status": "dry_run", "info": "Skipped actual API call"}

            func = getattr(self.exchange, method)
            return func(*args, **kwargs)

        except ccxt.NetworkError as e:
            self.logger.error(f"Network error on {method}: {e}")
            raise
        except ccxt.ExchangeError as e:
            self.logger.error(f"Exchange error on {method}: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error on {method}: {e}")
            raise

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        return self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

    def create_order(self, symbol, type, side, amount, price=None):
        return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price)

    def fetch_balance(self):
        return self._safe_request('fetch_balance')
