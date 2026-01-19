import logging
from engine.database import get_bot_status, reset_bot_after_tp
from config.settings import config

logger = logging.getLogger("StateSync")

def sync_bot_state(bot_id, exchange, db_status=None):
    """
    Synchronizes the local database state with the actual exchange state.
    
    Handles:
    1. Orphaned Orders: Bot thinks it's idle, but orders exist -> Cancel them.
    2. Ghost Trades: Bot thinks it's in a trade, but no orders exist -> Assume TP hit and close.
    
    Args:
        bot_id (int): The ID of the bot to sync.
        exchange (ExchangeInterface): The exchange interface instance.
        db_status (tuple, optional): Pre-fetched status from get_bot_status.
    """
    if db_status is None:
        db_status = get_bot_status(bot_id)
    
    if not db_status:
        logger.warning(f"Bot ID {bot_id} not found in DB. Skipping sync.")
        return

    # Unpack DB Status
    # Query: SELECT b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, ...
    name = db_status[0]
    pair = db_status[1]
    current_step = db_status[2]
    total_invested = db_status[3]
    target_tp = db_status[5]
    
    # Determine DB State
    is_in_trade = total_invested > 0

    logger.info(f"Syncing state for Bot {name} ({pair})...")

    try:
        # 1. Fetch Positions from Exchange (Futures only)
        # This is the single source of truth for "In Trade"
        has_exchange_position = False
        exchange_side = None
        
        if exchange.market_type in ['future', 'swap']:
            try:
                positions = exchange.exchange.fetch_positions([pair])
                for pos in positions:
                    contracts = float(pos.get('contracts', 0) or 0)
                    if contracts != 0:
                        has_exchange_position = True
                        exchange_side = pos.get('side')
                        break
            except Exception as e:
                logger.error(f"Failed to fetch positions for {name}: {e}")
        else:
            # Spot logic: Check base asset balance
            try:
                base_asset = pair.split('/')[0]
                balance = exchange.fetch_balance()
                free = float(balance.get(base_asset, {}).get('free', 0))
                if free > 0: # This is a bit naive for spot but better than nothing
                    has_exchange_position = True
            except Exception as e:
                logger.error(f"Failed to fetch spot balance for {name}: {e}")

        # 2. Fetch Open Orders
        try:
            open_orders = exchange.fetch_open_orders(pair)
        except Exception as e:
            logger.error(f"Failed to fetch open orders for {name}: {e}")
            return

        if open_orders is None: open_orders = []
        has_open_orders = len(open_orders) > 0

        # 3. Handle Mismatches
        
        # Scenario A: DB says IN TRADE, but Exchange has NO POSITION
        if is_in_trade and not has_exchange_position:
            logger.warning(f"State Mismatch for {name}: DB shows active position, but Exchange is EMPTY.")
            logger.info("   -> Action: Resetting DB state to IDLE (Likely manually closed or TP hit).")
            reset_bot_after_tp(bot_id, exit_price=0) # Reset to idle
            return

        # Scenario B: DB says IDLE, but Exchange HAS POSITION
        if not is_in_trade and has_exchange_position:
            logger.critical(f"CRITICAL Mismatch for {name}: DB says IDLE, but Exchange has ACTIVE POSITION!")
            logger.info("   -> Action: Bot cannot automatically manage external positions. Please manually close or re-sync.")
            # We don't auto-import positions for safety, just warn.

        # Scenario C: Orphaned Orders (Idle DB & No Position, but orders exist)
        if not is_in_trade and not has_exchange_position and has_open_orders:
            logger.warning(f"State Mismatch for {name}: DB is IDLE, but Exchange has orphaned orders.")
            logger.info("   -> Action: Cancelling orphaned orders.")
            try:
                exchange.cancel_all_orders(pair)
            except Exception: pass

        logger.info(f"Bot {name} state synchronized. (In Trade: {has_exchange_position}, Open Orders: {len(open_orders)})")



    except Exception as e:
        logger.error(f"Critical error during sync for {name}: {e}")
