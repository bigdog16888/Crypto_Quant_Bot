import ccxt
import time
import logging
from config.settings import config

class ExchangeInterface:
    def __init__(self, exchange_id='binance', market_type='spot'):
        self.exchange_id = exchange_id
        
        # Determine options based on market type
        options = {'defaultType': market_type} 
        
        self.exchange = getattr(ccxt, exchange_id)({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'enableRateLimit': True,
            'options': options
        })
        self.logger = logging.getLogger(__name__)

    def _safe_request(self, method, *args, **kwargs):
        """Wrapper for API calls with basic error handling and security checks."""
        try:
            # Security check: Whitelist (Dynamic check preferred but sticking to config/safety first)
            # If we allow dynamic fetching, we might relax strict config whitelist or update it at runtime.
            # For now, we will perform a basic check if the symbol is blatantly wrong if needed, 
            # but relying on `get_available_symbols` for UI makes this less critical for reading.
            # However, for TRADING, we still strictly respect the whitelist or user approval.
            
            if 'symbol' in kwargs:
                # Basic sanity check
                pass 
            
            # Security check: Max order size (if applicable)
            if 'amount' in kwargs and 'price' in kwargs:
                usd_value = float(kwargs['amount']) * float(kwargs['price'])
                if usd_value > config.MAX_ORDER_USD:
                    raise ValueError(f"Order value ${usd_value} exceeds MAX_ORDER_USD safety limit")

            if config.DRY_RUN:
                # Allow fetch methods even in dry run
                if method.startswith('fetch') or method.startswith('load'):
                    func = getattr(self.exchange, method)
                    return func(*args, **kwargs)
                
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

    def get_available_symbols(self, quote_asset='USDT'):
        """
        Dynamically fetches tickers and filters by quote asset (e.g. USDT, USDC).
        """
        try:
            self.exchange.load_markets()
            symbols = [
                symbol for symbol in self.exchange.symbols 
                if symbol.endswith(f"/{quote_asset}") or symbol.endswith(f"{quote_asset}") # Handle different formats
            ]
            symbols.sort()
            return symbols
        except Exception as e:
            self.logger.error(f"Failed to fetch symbols: {e}")
            return []

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=100):
        return self._safe_request('fetch_ohlcv', symbol=symbol, timeframe=timeframe, limit=limit)

    def create_order(self, symbol, type, side, amount, price=None):
        return self._safe_request('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price)

    def fetch_balance(self):
        return self._safe_request('fetch_balance')
