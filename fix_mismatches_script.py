from engine.exchange_interface import ExchangeInterface

def close_physical():
    try:
        ex = ExchangeInterface('future')
        positions = ex.fetch_positions()
        
        if positions is None:
            print("Failed to fetch positions.")
            return

        for p in positions:
            qty = p.get('contracts', 0)
            if qty != 0.0:
                sym = p['symbol']
                side = 'sell' if p['side'] == 'long' else 'buy'
                
                print(f"Closing {sym}: market {side} {abs(qty)}")
                try:
                    res = ex.create_order(sym, 'market', side, abs(qty), params={'reduceOnly': True})
                    print(f"✅ Success: {res}")
                except Exception as e:
                    print(f"❌ Failed to close {sym}: {e}")
                    
        print("Done closing positions.")
    except Exception as e:
        print(f"Fatal error: {e}")

if __name__ == '__main__':
    close_physical()
