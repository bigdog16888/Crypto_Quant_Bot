import pandas as pd
import pandas_ta as ta

# Create dummy data
data = {
    "open": [10, 11, 12, 11, 10, 9, 8, 9, 10],
    "high": [11, 12, 13, 12, 11, 10, 9, 10, 11],
    "low": [9, 10, 11, 10, 9, 8, 7, 8, 9],
    "close": [10.5, 11.5, 12.5, 11.5, 10.5, 9.5, 8.5, 9.5, 10.5],
    "volume": [100, 110, 120, 110, 100, 90, 80, 90, 100]
}
df = pd.DataFrame(data)

# Test an indicator
df.ta.rsi(length=5, append=True)

if "RSI_5" in df.columns:
    print("✅ pandas-ta is working correctly!")
    print(df.tail(2))
else:
    print("❌ pandas-ta failed to generate RSI column.")
