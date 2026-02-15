"""
WebSocket Event Handlers (Phase 7)

Processes real-time events from Binance WebSocket stream:
- Order updates (fill, cancel, new)
- Position updates
- Balance updates

Updates database state based on these events.
"""

import logging
from typing import Dict

logger = logging.getLogger("WSEventHandlers")


def handle_order_update(event: Dict):
    """
    Handle real-time order update from WebSocket.
    
    Event structure:
    {
        'event': 'order_update',
        'symbol': 'BTCUSDC',
        'side': 'BUY/SELL',
        'status': 'NEW/FILLED/CANCELED/EXPIRED',
        'order_id': 123456,
        'client_order_id': 'CQB_37_GRID_abc123',
        'price': 78000.0,
        'qty': 0.002,
        'filled_qty': 0.002,
        'avg_price': 78000.5,
        'realized_pnl': 12.50,
        'timestamp': 1234567890
    }
    """
    try:
        status = event.get('status')
        client_id = event.get('client_order_id', '')
        order_id = event.get('order_id')
        symbol = event.get('symbol')
        
        # Only process bot orders (tagged with CQB_)
        if not client_id.startswith('CQB_'):
            logger.debug(f"Ignoring non-bot order update: {order_id}")
            return
            
        # Parse bot ID from clientOrderId
        # Format: CQB_{bot_id}_{type}_{uuid}
        parts = client_id.split('_')
        if len(parts) < 3:
            logger.warning(f"Invalid clientOrderId format: {client_id}")
            return
            
        bot_id = int(parts[1])
        order_type = parts[2]  # ENTRY, TP, GRID
        
        # FUNDAMENTAL SAFETY CHECK: Is Bot Active?
        # If we process a fill for an inactive bot, we might trigger new orders (Grid/TP)
        from engine.database import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM bots WHERE id = ?", (bot_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row or not row[0]:
            logger.warning(f"⛔ WS IGNORING Event for INACTIVE Bot {bot_id} (ClientID: {client_id})")
            return
        
        if status == 'FILLED':
            _handle_order_filled(bot_id, order_type, event)
        elif status == 'CANCELED':
            _handle_order_canceled(bot_id, order_type, event)
        elif status == 'NEW':
            _handle_order_new(bot_id, order_type, event)
        elif status == 'EXPIRED':
            _handle_order_canceled(bot_id, order_type, event)
            
    except Exception as e:
        logger.error(f"Error handling order update: {e}")


# Module-level set to track notified order fills (prevents duplicates)
_notified_fills = set()
_notified_fills_max_size = 10000  # Prevent unbounded growth
_notified_fills_timestamps = {} # For rate limiting specific notification types

def _cleanup_notified_fills():
    """Cleanup notified fills set if it grows too large."""
    global _notified_fills
    if len(_notified_fills) > _notified_fills_max_size:
        # Keep most recent 50%, clear old ones
        logger.info(f"🧹 Cleaning up notified_fills set (size: {len(_notified_fills)})")
        _notified_fills = set(list(_notified_fills)[-5000:])

def _handle_order_filled(bot_id: int, order_type: str, event: Dict):
    """Process a filled order - update trade state."""
    from engine.database import (
        update_martingale_step, reset_bot_after_tp, 
        get_bot_order_ids, close_order_in_db, log_trade,
        add_notification
    )
    
    order_id = event.get('order_id')
    avg_price = event.get('avg_price', 0)
    filled_qty = event.get('filled_qty', 0)
    realized_pnl = event.get('realized_pnl', 0)
    symbol = event.get('symbol')
    
    logger.info(f"🎯 WS FILL: Bot {bot_id} {order_type} filled @ {avg_price} (PnL: ${realized_pnl:.2f})")
    logger.info(f"🔍 [DIAG-NOTIFICATION] About to add notification for {order_type} fill")
    
    # DEDUPLICATION: Check if we already notified for this order
    notification_key = f"{bot_id}_{order_id}_{order_type}"
    if notification_key in _notified_fills:
        logger.debug(f"⏭️ Skipping duplicate notification for {notification_key}")
        return
    _notified_fills.add(notification_key)
    _cleanup_notified_fills()  # Periodic cleanup
    
    if order_type == 'TP':
        # Take Profit hit - reset bot
        logger.info(f"✅ WS TP Hit for Bot {bot_id}! Resetting trade...")
        try:
            reset_bot_after_tp(bot_id, exit_price=avg_price)
            log_trade(bot_id, 'WS_TP_FILL', symbol, avg_price, filled_qty, realized_pnl, "TP")
            add_notification('success', f"💰 TP Hit for {symbol} (PnL ${realized_pnl:.2f})", bot_id)
        except Exception as e:
            logger.error(f"Failed to process TP fill for bot {bot_id}: {e}")
            
    elif order_type == 'GRID':
        # Grid order filled - increment step
        logger.info(f"📈 WS Grid Fill for Bot {bot_id}")
        try:
            # Get current step and increment
            from engine.database import get_bot_status
            trade_data = get_bot_status(bot_id)
            if trade_data:
                current_step = trade_data.get('current_step', 0)
                current_invested = float(trade_data.get('total_invested', 0))
                current_avg = float(trade_data.get('avg_entry_price', 0)) or avg_price
                
                # Calculate new average
                added_value = avg_price * filled_qty
                new_invested = current_invested + added_value
                new_avg = (current_avg * current_invested + added_value) / new_invested if new_invested > 0 else avg_price
                
                update_martingale_step(
                    bot_id, 
                    current_step + 1,
                    new_invested,
                    new_avg,
                    new_avg * 1.015  # Recalculate TP
                )
                log_trade(bot_id, 'WS_GRID_FILL', symbol, avg_price, filled_qty, 0, "GRID", current_step + 1)
                add_notification('info', f"📉 Grid Fill for {symbol} (Step {current_step+1})", bot_id)
        except Exception as e:
            logger.error(f"Failed to process Grid fill for bot {bot_id}: {e}")
            
    elif order_type == 'ENTRY':
        # Entry order filled - start trade
        logger.info(f"🚀 WS Entry Fill for Bot {bot_id}")
        try:
            update_martingale_step(
                bot_id,
                0,
                avg_price * filled_qty,
                avg_price,
                avg_price * 1.015  # Initial TP
            )
            log_trade(bot_id, 'WS_ENTRY_FILL', symbol, avg_price, filled_qty, 0, "ENTRY")
            # RATE LIMITING: Check if we just notified for this bot/symbol
            # This prevents the "Entry Fill" flood if the WS stream replays or loops
            import time
            current_time = time.time()
            fill_key = f"ENTRY_FILL_{bot_id}_{symbol}"
            last_fill_time = _notified_fills_timestamps.get(fill_key, 0)
            
            if (current_time - last_fill_time) > 10.0: # Only notify once per 10 seconds for same bot/symbol entry
                add_notification('info', f"🚀 Entry Fill for {symbol}", bot_id)
                _notified_fills_timestamps[fill_key] = current_time
            else:
                logger.debug(f"Combinator: Skipping duplicate Entry Fill notification for {symbol} (Time delta: {current_time - last_fill_time:.2f}s)")
        except Exception as e:
            logger.error(f"Failed to process Entry fill for bot {bot_id}: {e}")
    
    # Close order in DB
    try:
        close_order_in_db(order_id)
    except Exception as e:
        logger.debug(f"Could not close order {order_id} in DB: {e}")


def _handle_order_canceled(bot_id: int, order_type: str, event: Dict):
    """Process a canceled order - update DB."""
    from engine.database import close_order_in_db
    
    order_id = event.get('order_id')
    logger.info(f"❌ WS Cancel: Bot {bot_id} {order_type} order {order_id} canceled")
    
    try:
        close_order_in_db(order_id)
    except Exception as e:
        logger.debug(f"Could not close canceled order {order_id} in DB: {e}")


def _handle_order_new(bot_id: int, order_type: str, event: Dict):
    """Process a new order confirmation - can be used for logging."""
    order_id = event.get('order_id')
    price = event.get('price')
    qty = event.get('qty')
    
    logger.debug(f"📝 WS New Order: Bot {bot_id} {order_type} #{order_id} @ {price}")


def handle_position_update(event: Dict):
    """
    Handle real-time position update from WebSocket.
    
    Event structure:
    {
        'event': 'position_update',
        'symbol': 'BTCUSDC',
        'side': 'LONG/SHORT',
        'position_amt': 0.006,
        'entry_price': 78000.0,
        'unrealized_pnl': -12.50,
        'margin_type': 'cross',
        'timestamp': 1234567890
    }
    """
    try:
        symbol = event.get('symbol')
        position_amt = event.get('position_amt', 0)
        entry_price = event.get('entry_price', 0)
        unrealized_pnl = event.get('unrealized_pnl', 0)
        
        # Position amount of 0 means closed
        if position_amt == 0:
            logger.info(f"📊 WS Position Closed: {symbol}")
            # Could trigger ghost detection or cleanup here
        else:
            logger.debug(f"📊 WS Position: {symbol} {position_amt} @ {entry_price} (uPnL: ${unrealized_pnl:.2f})")
            
    except Exception as e:
        logger.error(f"Error handling position update: {e}")
