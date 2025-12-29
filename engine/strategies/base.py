from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    """
    Abstract Base Class for all trading strategies.
    Enforces a common interface for signal generation and parameter management.
    """
    def __init__(self, name: str, params: dict = None):
        self.name = name
        self.params = params if params else {}

    @abstractmethod
    def check_signals(self, market_data) -> tuple[bool, bool]:
        """
        Analyzes market data and returns buy/sell signals.
        
        Args:
            market_data (pd.DataFrame): DataFrame containing OHLCV data.
                                        Must contain 'open', 'high', 'low', 'close', 'volume'.
        
        Returns:
            tuple[bool, bool]: (buy_signal, sell_signal)
        """
        pass
