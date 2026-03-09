import os

def update_all_bots_stop_cycle(enabled: bool) -> bool:
    """
    Toggles 'Stop After Cycle' (post_exit_stop) for ALL active bots.
    """
    try:
        import logging
        logger = logging.getLogger("TradingControls")
        from engine.database import get_all_bots, update_bot_config_value
        
        bots = get_all_bots()
        success_count = 0
        
        for bot in bots:
            bot_id = bot[0]
            # is_active is at index 9 (0-indexed) in get_all_bots SELECT: 
            # id, name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config, is_active, status
            is_active = bot[9] 
            if is_active:
                update_bot_config_value(bot_id, 'post_exit_stop', enabled)
                success_count += 1
        
        logger.info(f"🌐 Global Stop After Cycle set to {enabled} for {success_count} bots.")
        return True
    except Exception as e:
        import logging
        logger = logging.getLogger("TradingControls")
        logger.error(f"Failed to set global stop after cycle: {e}")
        return False
