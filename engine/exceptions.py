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

class GTXRejected(ExchangeError):
    """Raised when a Post-Only (GTX) order is rejected by the exchange."""
    pass

class OrderAlreadyGoneError(ExchangeError):
    """Cancel target already terminal on the exchange (filled/cancelled/expired).

    Raised by _raw_request() when a DELETE /fapi/v1/order returns 400 with a
    Binance -2011 ("Unknown order sent") or -2013 ("Order does not exist")
    body. Distinct from CancelFailedError so callers can treat
    "already gone" as a success-like outcome without a verify-GET round trip.
    Never collapsed to None (silent-cancel-swallow defect, 2026-09-10 P1).
    """
    def __init__(self, message: str, error_code: int = None, raw_body: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.raw_body = raw_body

class CancelFailedError(ExchangeError):
    """Cancel did NOT land and the exchange could not confirm the order gone.

    Raised by cancel_order() when the DELETE fails for a non-gone reason, OR
    when the post-failure verify-GET shows the order still live / status
    unverifiable. Callers must NOT mark the DB row cancelled and must feed
    the failure into the consecutive-failure escalation counter.
    (silent-cancel-swallow defect, 2026-09-10 P1).
    """
    def __init__(self, message: str, error_code: int = None, raw_body: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.raw_body = raw_body
