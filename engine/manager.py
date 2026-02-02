from datetime import datetime
import math

def calculate_early_exit_decay(
    basket_start_time: datetime,
    current_time: datetime,
    total_orders: int,
    initial_tp: float,
    break_even: float,
    settings: dict
) -> float:
    """
    Calculates the adjusted Take Profit price based on Early Exit decay logic.
    User Requirement: "reduce takeprofit by 30% each 15 minutes untill brake even"
    """
    if not settings.get('UseEarlyExit', False):
        return initial_tp
        
    # Standard Strategy Params
    start_hours = settings.get('EEStartHours', 0.0)
    hours_pc = settings.get('EEHoursPC', 0.0) # Percent per hour
    
    # Accelerated Params (Custom)
    interval_mins = settings.get('DecayIntervalMins', 60.0)
    decay_per_interval = settings.get('DecayPercentPerInterval', 0.0) / 100.0
    
    # Calculate duration
    duration_seconds = (current_time - basket_start_time).total_seconds()
    duration_hours = duration_seconds / 3600.0
    duration_mins = duration_seconds / 60.0
    
    ee_pc = 0.0
    
    # 1. Standard Time-based decay
    if duration_hours > start_hours:

        ee_pc += (duration_hours - start_hours) * (hours_pc / 100.0)
        
    # 2. Accelerated Interval-based decay (User Style: 30% per 15 mins)
    if decay_per_interval > 0:
        intervals_passed = duration_mins / interval_mins
        ee_pc += intervals_passed * decay_per_interval
        
    # 3. Level-based decay
    start_level = settings.get('EEStartLevel', 5)
    level_pc = settings.get('EELevelPC', 0.0) / 100.0
    if total_orders >= start_level:
        ee_pc += (total_orders - start_level + 1) * level_pc
        
    # Calculate Decay Factor (1.0 = No Decay, 0.0 = Full Decay to BE)
    decay_factor = 1.0 - ee_pc
    
    allow_loss = settings.get('EEAllowLoss', False)
    if not allow_loss and decay_factor < 0:
        decay_factor = 0.0 # Floor at BreakEven
    
    # New TP is weighted average of InitialTP and BE
    # Logic: NewTP = BE + (InitialTP - BE) * DecayFactor
    adjusted_tp = break_even + (initial_tp - break_even) * decay_factor
    
    return adjusted_tp

def check_moving_profit_target(current_price: float, average_price: float, target_price: float, current_sl: float, direction: str, settings: dict) -> float:
    """
    Checks if the profit target has been reached to "lock in" profit.
    Equivalent to 'MaximizeProfit' logic.

    """
    if not settings.get('MaximizeProfit', False):
        return 0.0

    profit_set = settings.get('ProfitSet', 0.5)
    
    # Calculate the Moving Profit Target Price (TPbMP)
    # Logic: BE + (TP - BE) * ProfitSet
    # Example: BE=1.00, TP=1.10, Set=0.5 -> 1.05.
    moving_target = average_price + (target_price - average_price) * profit_set
    
    new_sl = 0.0
    
    if direction == 'buy':
        # If Price > MovingTarget, move SL to MovingTarget
        if current_price > moving_target:
            if current_sl == 0 or current_sl < moving_target:
                new_sl = moving_target
    elif direction == 'sell':
        # For Sell, TP is lower than BE.
        if current_price < moving_target:
            if current_sl == 0 or current_sl > moving_target:
                new_sl = moving_target
                
    return new_sl

def check_hedge_entry(drawdown_percent: float, open_levels: int, settings: dict) -> dict | None:
    """
    Determines if a hedge trade should be opened based on drawdown or levels.
    """
    if not settings.get('UseHedge', False):
        return None
        
    hedge_start = settings.get('HedgeStart', 20.0)
    use_dd = settings.get('HedgeTypeDD', True)
    
    trigger = False
    if use_dd:
        if drawdown_percent >= hedge_start:
            trigger = True
    else:
        if open_levels >= int(hedge_start):
            trigger = True
            
    if not trigger:
        return None
        
    return {
        'action': 'open_hedge',
        'trigger_value': drawdown_percent if use_dd else open_levels,
        'size_mult': settings.get('LotMultHedge', 1.0)
    }

def calculate_hedge_lot(main_basket_lots: float, settings: dict) -> float:
    """
    Calculates the lot size for the hedge trade.
    """
    mult = settings.get('LotMultHedge', 1.0)
    return main_basket_lots * mult

def _check_trailing_stop(bot_id, bot_name, direction, current_price, target_tp_price, avg_entry_price, settings, logger):
    """
    Evaluates Trailing Profit conditions.
    Returns: (tp_hit: bool, is_trailing_exit: bool)
    """
    tp_hit = False
    is_trailing_exit = False
    trail_percent = float(settings.get('ProfitSet', 0.5))

    if direction == 'LONG':
        stored_peak = float(settings.get('trailing_peak', 0.0))
        peak_price = max(stored_peak, current_price, target_tp_price)
        
        if peak_price > stored_peak and peak_price > target_tp_price:
            try:
                from engine.database import update_bot_config_value
                update_bot_config_value(bot_id, 'trailing_peak', peak_price)
                logger.info(f"📈 Trailing Peak for {bot_name} updated to {peak_price}")
            except Exception as e:
                logger.error(f"Failed to update trailing peak: {e}")
        
        stop_price = target_tp_price + (peak_price - target_tp_price) * trail_percent
        
        if current_price <= stop_price and current_price >= avg_entry_price:
             tp_hit = True
             is_trailing_exit = True
             logger.info(f"Trailing Stop Hit for {bot_name}: Price {current_price} <= Stop {stop_price} (Peak {peak_price})")

    elif direction == 'SHORT':
         stored_peak_s = float(settings.get('trailing_peak', 99999999.0))
         if stored_peak_s == 0.0: stored_peak_s = 99999999.0
         
         peak_price = min(stored_peak_s, current_price, target_tp_price)
         
         if peak_price < stored_peak_s and peak_price < target_tp_price:
             try:
                from engine.database import update_bot_config_value
                update_bot_config_value(bot_id, 'trailing_peak', peak_price)
                logger.info(f"📉 Trailing Peak for {bot_name} updated to {peak_price}")
             except: pass
         
         stop_price = target_tp_price - (target_tp_price - peak_price) * trail_percent
         
         if current_price >= stop_price and current_price <= avg_entry_price:
             tp_hit = True
             is_trailing_exit = True
             logger.info(f"Trailing Stop Hit for {bot_name}: Price {current_price} >= Stop {stop_price} (Peak {peak_price})")
             
    return tp_hit, is_trailing_exit

def manage_trade(bot_id, bot_name, pair, direction, settings, trade_data, current_price, strategy, exchange_interface, open_orders=None):
    """
    Core trade management logic called by the runner.
    trade_data: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
    open_orders: Pre-fetched list of open orders from exchange (passed from process_bot for efficiency and consistency).
    Returns: dict with missions for the runner to execute (e.g. {'action': 'TP', 'price': ...})
    """
    import logging
    from engine.database import get_bot_order_ids
    from engine.exchange_interface import normalize_symbol
    logger = logging.getLogger("TradeManager")

    # Safety unpack (handle cases where tuple length varies during migration)
    if len(trade_data) >= 8:
        # Compatibility unpacking
        # (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, *basket_start)
        _, _, _current_step, _total_invested, _avg_entry_price, _target_tp_price, _, _ = trade_data[:8]
        
        # Enforce type safety to prevent NoneType crashes in Logic
        current_step = int(_current_step) if _current_step is not None else 0
        total_invested = float(_total_invested) if _total_invested is not None else 0.0
        avg_entry_price = float(_avg_entry_price) if _avg_entry_price is not None else 0.0
        target_tp_price = float(_target_tp_price) if _target_tp_price is not None else 0.0
    else:
        return {'action': 'none'}
    
    # --- STRUCTURAL FIX (v0.6.1): Use Passed Snapshot & Strict ID Matching ---
    # REMOVED duplicate fetch_open_orders call (was lines 203-214).
    # Use pre-fetched `open_orders` for consistency.
    # Check for THIS bot's orders using tracked IDs, not generic side matching.
    force_maintain = False
    try:
        if open_orders is None:
            # Fallback if not passed (e.g., direct call for testing)
            open_orders = exchange_interface.fetch_open_orders(pair) or []
            logger.warning(f"manage_trade: open_orders not passed for {bot_name}, fetching (suboptimal).")

        bot_order_ids = get_bot_order_ids(bot_id)
        my_tp_id = bot_order_ids.get('tp_order_id')
        my_grid_ids = [o.get('order_id') for o in bot_order_ids.get('grid_orders', []) if o.get('status') == 'open']

        # Check: Does MY TP order exist on exchange?
        has_my_tp = any(o.get('id') == my_tp_id for o in open_orders) if my_tp_id else False
        # Check: Does MY Grid order exist?
        has_my_grid = any(o.get('id') in my_grid_ids for o in open_orders) if my_grid_ids else False
        
        # Scenario: TP exists but ID mismatch (unlikely but possible if re-placed by hand)
        # We perform a "fallback" check by price/side if ID isn't found
        if not has_my_tp and not settings.get('MaximizeProfit', False):
            tp_side = 'sell' if direction == 'LONG' else 'buy'
            has_my_tp = any(
                normalize_symbol(o.get('symbol')) == normalize_symbol(pair) and 
                o.get('side') == tp_side and 
                abs(float(o.get('price') or 0) - target_tp_price) / target_tp_price < 0.001 
                for o in open_orders
            )

        # If EITHER is missing, we need to maintain (re-place) orders.
        # Note: TP might be intentionally 0 if MaximizeProfit is active (trailing stop mode).
        if not settings.get('MaximizeProfit', False):
            force_maintain = not has_my_tp
        force_maintain = force_maintain or not has_my_grid
        
        if force_maintain:
            logger.info(f"🔧 Force Maintain for {bot_name}: My TP Found={has_my_tp}, My Grid Found={has_my_grid}")
    except Exception as e:
        logger.error(f"Error checking bot order IDs for {bot_name}: {e}")
        force_maintain = True # Err on side of caution

    # --- SELF-HEALING: Fix 0.00 Target TP (v0.6.3) ---
    # If bot is in trade but Target TP is 0 (and not using Trailing Stop), we must recalculate it.
    if current_step >= 0 and target_tp_price <= 0 and not settings.get('MaximizeProfit', False):
        try:
            logger.warning(f"🚑 Self-Healing: Detected 0.00 Target TP for {bot_name}. Recalculating...")
            
            # Recalculate based on strategy settings
            tp_type = settings.get('TakeProfitType', 'USD')
            healed_tp = 0.0
            
            if tp_type == 'Percent':
                tp_pct = settings.get('TakeProfitPct', 1.0) / 100.0
                healed_tp = avg_entry_price * (1.0 + tp_pct) if direction == 'LONG' else avg_entry_price * (1.0 - tp_pct)
            else:
                target_usd = settings.get('TakeProfitBase', 10.0)
                # Estimate qty
                est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
                if est_qty > 0:
                    dist = target_usd / est_qty
                    healed_tp = avg_entry_price + dist if direction == 'LONG' else avg_entry_price - dist
            
            if healed_tp > 0:
                # Update DB immediately
                from engine.database import update_trade_tp_price
                update_trade_tp_price(bot_id, healed_tp)
                target_tp_price = healed_tp # Use valid TP for this cycle
                logger.info(f"✅ Self-Healing: Restored Target TP to {healed_tp:.4f}")
                
        except Exception as e:
            logger.error(f"Self-Healing Failed: {e}")

    # 1. Check Take Profit
    effective_tp = target_tp_price
    
    # Early Exit Decay Logic
    if settings.get('UseEarlyExit', False) and len(trade_data) > 8:
        basket_start_time = trade_data[8]
        if basket_start_time > 0:
            import time
            from datetime import datetime
            
            start_dt = datetime.fromtimestamp(basket_start_time)
            now_dt = datetime.fromtimestamp(time.time())
            
            effective_tp = calculate_early_exit_decay(
                basket_start_time=start_dt,
                current_time=now_dt,
                total_orders=current_step + 1,
                initial_tp=target_tp_price,
                break_even=avg_entry_price,
                settings=settings
            )
            
            if effective_tp < target_tp_price * 0.999:
                logger.debug(f"EE: {bot_name} TP decayed from {target_tp_price} to {effective_tp}")

    # --- TRAILING PROFIT LOGIC (Replaces Moving Profit) ---
    maximize_profit = settings.get('MaximizeProfit', False)
    tp_hit = False
    is_trailing_exit = False
    
    if maximize_profit:
        tp_hit, is_trailing_exit = _check_trailing_stop(
            bot_id, bot_name, direction, current_price, 
            target_tp_price, avg_entry_price, settings, logger
        )

    # Standard TP Trigger (Only if NOT optimizing profit)
    else:
        if direction == 'LONG':
            if current_price >= effective_tp: tp_hit = True
        else:
            if current_price <= effective_tp: tp_hit = True
        
    if tp_hit:
        reason = "Trailing Stop" if is_trailing_exit else "Take Profit Target"
        logger.info(f"{reason} Hit for {bot_name}! Signal to close at {current_price}")
        est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
        pnl = (current_price - avg_entry_price) * est_qty if direction == 'LONG' else (avg_entry_price - current_price) * est_qty
        
        # CLEAR Locked ATR on exit
        try:
             from engine.database import update_bot_config_value
             update_bot_config_value(bot_id, 'locked_atr', None)
        except: pass
        
        return {
            'action': 'tp_hit',
            'bot_id': bot_id, 'bot_name': bot_name, 'pair': pair,
            'direction': direction, 'exit_price': current_price,
            'qty': est_qty, 'pnl': pnl, 'current_step': current_step,
            'avg_entry_price': avg_entry_price, 'total_invested': total_invested
        }

    # 2. Check Next Martingale Grid Order
    max_steps = int(settings.get('max_steps', 10))
    grid_mission_active = current_step < max_steps
    
    # Initialize defaults to prevent UnboundLocalError on exception
    grid_price, grid_qty, grid_step, grid_amount_usd = None, 0, 0, 0
    next_order_price, new_avg, new_tp = 0, avg_entry_price, 0

    try:
        if grid_mission_active:
            # Fetch Last Grid Price for Incremental Calculation
            # (Allows Spacing from Last Order instead of Avg)
            last_grid_price = 0.0
            try:
                from engine.database import get_last_filled_order
                last_fill = get_last_filled_order(bot_id)
                if last_fill: last_grid_price = float(last_fill['price'])
            except Exception as e:
                logger.warning(f"Failed to fetch last grid price: {e}")

            # Load locked ATR from settings (persistence)
            saved_atr = settings.get('locked_atr')
            if saved_atr:
                strategy.locked_atr = float(saved_atr)

            next_order_price = strategy.calculate_next_grid_price(
                direction, current_price, avg_entry_price, current_step, 
                strategy.last_market_data if hasattr(strategy, 'last_market_data') else None,
                last_grid_price=last_grid_price
            )
            
            # Persist Locked ATR if newly locked
            if strategy.locked_atr and strategy.locked_atr != saved_atr:
                from engine.database import update_bot_config_value
                update_bot_config_value(bot_id, 'locked_atr', strategy.locked_atr)
                logger.info(f"🔒 Locked ATR for {bot_name}: {strategy.locked_atr:.4f}")
            
            next_step = current_step + 1
            added_investment = strategy.calculate_lot_size(next_step, 0)
            new_total = total_invested + added_investment
            new_avg = (avg_entry_price * total_invested + next_order_price * added_investment) / new_total
            
            logger.info(f"DEBUG_GRID: Step {next_step} | Price: {next_order_price} | Qty: {added_investment/next_order_price:.4f} | ATR: {strategy.locked_atr}")

            try:
                from engine.database import log_trade
                log_trade(bot_id, 'DEBUG_LOG', pair, next_order_price, 0, 0, "GRID_CALC", next_step, 0, f"Grid Calc: {next_order_price:.4f}")
            except: pass
            
            grid_price, grid_qty, grid_step, grid_amount_usd = next_order_price, added_investment / next_order_price, next_step, added_investment
        else:
            next_order_price, new_avg = 0, avg_entry_price
            grid_price, grid_qty, grid_step, grid_amount_usd = None, 0, 0, 0

        # Calculate TP
        tp_type = settings.get('TakeProfitType', 'USD')
        if tp_type == 'Percent':
            tp_pct = settings.get('TakeProfitPct', 1.0) / 100.0
            new_tp = new_avg * (1.0 + tp_pct) if direction == 'LONG' else new_avg * (1.0 - tp_pct)
        else:
            target_usd = settings.get('TakeProfitBase', 10.0)
            est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
            if grid_mission_active: 
                hypo_total = total_invested + strategy.calculate_lot_size(current_step + 1, 0)
                est_qty = hypo_total / new_avg
            if est_qty > 0:
                dist = target_usd / est_qty
                new_tp = new_avg + dist if direction == 'LONG' else new_avg - dist
            else: new_tp = 0

        # MISSION: MAINTAIN ORDERS
        # If force_maintain is True (orders missing on exchange), we trigger the mission
        
        tp_limit_price = effective_tp
        # If Trailing Profit is active, we do NOT want a physical Limit TP order.
        # We handle exit via the "Smart Chase" trigger in Logic Block 1.
        if settings.get('MaximizeProfit', False):
            tp_limit_price = 0.0
            
        return {
            'action': 'maintain_orders',
            'bot_id': bot_id, 'bot_name': bot_name, 'pair': pair,
            'direction': direction, 'current_price': current_price,
            'grid_price': grid_price, 'grid_step': grid_step,
            'grid_amount_usd': grid_amount_usd, 'grid_qty': grid_qty,
            'tp_price': tp_limit_price,
            'tp_qty': total_invested / avg_entry_price if avg_entry_price > 0 else 0,
            'future_avg': new_avg, 'future_tp': new_tp
        }

            
    except Exception as e:
        logger.error(f"Error checking grid for {bot_name}: {e}")
        try:
            from engine.database import log_trade
            log_trade(bot_id, 'DEBUG_ERR', pair, 0, 0, 0, "GRID_CALC_ERR", current_step, 0, str(e))
        except: pass

    # 3. Check Hedge
    drawdown_pc = 0.0
    if avg_entry_price > 0:
        if direction == 'LONG':
            drawdown_pc = (avg_entry_price - current_price) / avg_entry_price * 100
        else:
            drawdown_pc = (current_price - avg_entry_price) / avg_entry_price * 100

    hedge_trigger = check_hedge_entry(drawdown_pc, current_step, settings)
    if hedge_trigger:
        hedge_size = total_invested * hedge_trigger.get('size_mult', 1.0)
        logger.warning(f"🛡️ Bot {bot_name} - HEDGE TRIGGERED! Size: ${hedge_size} at {current_price}")
        
        # MISSION: OPEN HEDGE
        return {
            'action': 'hedge_open',
            'bot_id': bot_id,
            'bot_name': bot_name,
            'pair': pair,
            'direction': direction,
            'price': current_price,
            'amount_usd': hedge_size,
            'qty': hedge_size / current_price if current_price > 0 else 0,
            'step': current_step,
            'drawdown_pct': drawdown_pc
        }

    return {'action': 'none'}

def emergency_close_all(exchange_interface, bots_in_trouble):
    """
    Kills all orders and closes all positions for specified bots.
    If bots_in_trouble is empty, it should close EVERYTHING.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.warning("🚨 EMERGENCY CLOSE TRIGGERED 🚨")
    
    # 1. Fetch balance/positions to find what's open if bots_in_trouble not provided
    # For now, we assume we iterate through the DB's active bots
    for bot in bots_in_trouble:
        bot_id, pair = bot
        logger.info(f"Closing all for Bot {bot_id} on {pair}")
        
        # Cancel all open orders for this pair
        try:
            exchange_interface.exchange.cancel_all_orders(pair)
            logger.info(f"All orders canceled for {pair}")
        except Exception as e:
            logger.error(f"Failed to cancel orders for {pair}: {e}")
            
        # Market close position
        # This requires fetching current position size
        # TODO: Implement actual market liquidation logic
