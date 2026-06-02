import sys
sys.path.insert(0, '.')
import pandas as pd
from engine.exchange_interface import ExchangeInterface

def main():
    ex = ExchangeInterface()
    print("Fetching SUI/USDC trades from exchange...")
    try:
        trades = ex.fetch_my_trades('SUI/USDC:USDC', limit=50)
        df = pd.DataFrame(trades)
        if not df.empty:
            cols = ['datetime', 'id', 'order', 'side', 'price', 'amount', 'cost']
            # Find matching column names
            present_cols = [c for c in cols if c in df.columns]
            if 'timestamp' in df.columns and 'datetime' not in df.columns:
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
                if 'datetime' not in present_cols:
                    present_cols.insert(0, 'datetime')
            print(df[present_cols].to_string())
        else:
            print("No trades returned from exchange.")
    except Exception as e:
        print(f"Error fetching trades: {e}")

if __name__ == '__main__':
    main()
