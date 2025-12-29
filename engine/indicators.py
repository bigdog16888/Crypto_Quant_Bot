import pandas as pd
import numpy as np

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI)
    """
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)

    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val.fillna(50)

def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Commodity Channel Index (CCI)
    """
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(window=period).mean()
    mad = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean())
    
    cci_val = (tp - sma_tp) / (0.015 * mad)
    return cci_val.fillna(0)

def bollinger_bands(close: pd.Series, period: int = 20, deviation: float = 2.0):
    """
    Bollinger Bands
    Returns: upper, mid, lower Series
    """
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    
    upper = mid + (std * deviation)
    lower = mid - (std * deviation)
    
    return upper.fillna(close), mid.fillna(close), lower.fillna(close)

def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 5, d_period: int = 3, slowing: int = 3):
    """
    Stochastic Oscillator
    Returns: %K, %D Series    
    """
    # Lowest Low and Highest High over k_period
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    
    # Fast %K
    fast_k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    
    # Slowing %K (moving average of Fast %K)
    if slowing > 1:
        k = fast_k.rolling(window=slowing).mean()
    else:
        k = fast_k
        
    # %D (moving average of %K)
    d = k.rolling(window=d_period).mean()
    
    return k.fillna(50), d.fillna(50)

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    Moving Average Convergence Divergence (MACD)
    Returns: macd_line, signal_line
    """
    exp1 = close.ewm(span=fast, adjust=False).mean()
    exp2 = close.ewm(span=slow, adjust=False).mean()
    
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    
    return macd_line.fillna(0), signal_line.fillna(0)
