
class BotError(Exception):
    """Base exception for all Bot errors."""
    pass

class ExchangeError(BotError):
    """Base for exchange-related errors."""
    pass

class InsufficientFundsError(ExchangeError):
    """Raised when the exchange reports insufficient balance."""
    pass

class OrderNotFoundError(ExchangeError):
    """Raised when an order ID cannot be found on the exchange."""
    pass

class APIError(ExchangeError):
    """Raised when the API returns a generic error."""
    pass

class NetworkError(ExchangeError):
    """Raised for network/connectivity issues."""
    pass

class ConfigurationError(BotError):
    """Raised for invalid bot configuration."""
    pass
