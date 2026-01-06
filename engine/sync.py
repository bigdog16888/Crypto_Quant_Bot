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

    logger.info(f"🔄 Syncing state for Bot {name} ({pair})...")

    try:
        # Fetch Open Orders from Exchange
        # In a real scenario, this hits the API. In DRY_RUN, it might return None or [] depending on mock.
        # We wrap in try-except to prevent startup crashes if API is down.
        try:
            open_orders = exchange.fetch_open_orders(pair)
        except Exception as e:
            logger.error(f"Failed to fetch open orders for {name}: {e}")
            return # Cannot sync without exchange data

        if open_orders is None:
            open_orders = []
            
        has_open_orders = len(open_orders) > 0
        
        # --- Synchronization Logic ---

        # Case 1: Orphaned Orders
        # DB says "No Trade" (Idle), but Exchange has Open Orders.
        if not is_in_trade and has_open_orders:
            logger.warning(f"⚠️ State Mismatch for {name}: DB says IDLE, but Exchange has {len(open_orders)} OPEN ORDERS.")
            logger.info("   -> Action: Cancelling orphaned orders to reset state.")
            
            try:
                exchange.cancel_all_orders(pair)
                logger.info(f"   ✅ Orphaned orders cancelled for {name}.")
            except Exception as e:
                logger.error(f"   ❌ Failed to cancel orphans for {name}: {e}")

        # Case 2: Ghost Trades
        # DB says "In Trade", but Exchange has NO Open Orders.
        # Implication: The TP (Take Profit) order likely filled while the bot was offline.
        # Assumption: If we are in a trade, we SHOULD have either a TP order or a Step Limit order.
        elif is_in_trade and not has_open_orders:
            logger.warning(f"⚠️ State Mismatch for {name}: DB says IN TRADE (Invested: {total_invested}), but Exchange has NO ORDERS.")
            logger.info("   -> Action: Assuming TP hit. Marking trade as closed in DB.")
            
            # We use the target_tp_price as the exit price since we can't easily fetch the exact fill price without 'fetch_my_trades'
            exit_price = target_tp if target_tp > 0 else 0
            
            reset_bot_after_tp(bot_id, exit_price=exit_price)
            logger.info(f"   ✅ Trade marked as closed for {name} (Saved Exit Price: {exit_price}).")
            
        else:
            # States match (roughly)
            # - Idle & No Orders
            # - In Trade & Has Orders
            logger.info(f"✅ Bot {name} state is synchronized. (In Trade: {is_in_trade}, Open Orders: {len(open_orders)})")

    except Exception as e:
        logger.error(f"Critical error during sync for {name}: {e}")
