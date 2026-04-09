import sys
sys.path.insert(0, '.')
import logging

logging.basicConfig(level=logging.DEBUG)

def check():
    from engine.database import get_connection
    from engine.runner import BotRunner

    print("Initializing BotRunner...")
    runner = BotRunner()
    executor = runner._bot_executor

    conn = get_connection()
    cur = conn.cursor()
    
    # 0=id, 1=name, 2=pair, 3=direction, 4=strategy_type, 5=config_json, ...
    cur.execute('SELECT id, name, pair, direction, strategy_type, config_json, total_invested, current_step, rsi_limit, status, base_size, martingale_multiplier FROM bots WHERE status=\'active\'')
    active_bots = cur.fetchall()

    print('Active bots count:', len(active_bots))

    for b in active_bots:
        bid = b[0]
        name = b[1]
        print(f'\n--- Testing Bot {bid} ({name}) ---')
        try:
            # Recreate the snapshot dict structure runner uses
            from engine.exchange_interface import ExchangeInterface
            ex = ExchangeInterface()
            tick = ex.fetch_ticker(b[2])
            snapshot = {'tickers': {b[2]: tick}}
            
            executor.process_bot(b, exchange_snapshot=snapshot)
        except Exception as e:
            print(f'Error: {e}')
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    check()
