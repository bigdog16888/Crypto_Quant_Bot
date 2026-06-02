import sys
sys.path.insert(0, '.')
import asyncio
import pandas as pd
from engine.exchange_interface import ExchangeInterface

async def main():
    ex = ExchangeInterface()
    await ex.initialize()
    
    # Check if exchange client is available
    if hasattr(ex, 'exchange') and ex.exchange:
        print("Fetching SUI/USDC trades from exchange...")
        try:
            trades = await ex.exchange.fetch_my_trades('SUI/USDC:USDC', limit=50)
            df = pd.DataFrame(trades)
            if not df.empty:
                # Keep important columns
                cols = ['datetime', 'id', 'order', 'side', 'price', 'amount', 'cost']
                print(df[cols].to_string())
            else:
                print("No trades returned from exchange.")
        except Exception as e:
            print(f"Error fetching trades: {e}")
    else:
        print("Exchange client not initialized or not found.")
        
    await ex.close()

asyncio.run(main())
