"""
FIX FOR reset_bot_after_tp to properly log to trade_history

Critical: When TP is detected (either online or offline via sync),
         we MUST log it to trade_history for proper tracking.
"""
import logging
import time
from engine.database import get_connection, log_trade

logger = logging.getLogger("TradeManager")

def reset_bot_after_tp_v2(bot_id, exit_price=0, calculate_pnl=True):
    """
    Resets the trade stats after a Take Profit (TP) hit, saving exit metadata.
    V2: Adds trade_history logging with PnL calculation.

    Args:
        bot_id (int): Bot ID to reset
        exit_price (float): Price at which position was closed
        calculate_pnl (bool): Whether to calculate and log PnL (default True)
    """
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Fetch current trade state before resetting
        cursor.execute('''
            SELECT t.total_invested, t.avg_entry_price, t.target_tp_price,
                   b.name, b.pair, b.direction, t.current_step
            FROM trades t
            JOIN bots b ON t.bot_id = b.id
            WHERE b.id = ?
        ''', (bot_id,))
        result = cursor.fetchone()

        if not result:
            logger.error(f"Bot {bot_id} not found in trades table during TP reset")
            return

        total_invested, avg_entry_price, target_tp_price, bot_name, pair, direction, current_step = result

        if total_invested <= 0:
            logger.warning(f"Bot {bot_name} already at zero investment, skipping TP reset")
            return

        # Calculate PnL if requested
        pnl = 0.0
        if calculate_pnl and exit_price > 0 and avg_entry_price > 0:
            # Estimate quantity (in base currency)
            # Crypto: Base/Quote, Investment is Quote
            # Qty = Investment / Price
            est_qty = total_invested / avg_entry_price

            if direction.upper() == 'LONG':
                pnl = (exit_price - avg_entry_price) * est_qty
            else:  # SHORT
                pnl = (avg_entry_price - exit_price) * est_qty

            logger.info(f"PnL Calculation for {bot_name}: Exit=${exit_price:.4f}, Entry=${avg_entry_price:.4f}, PnL=${pnl:.2f}")

        # Log the TP hit to trade_history BEFORE resetting
        log_trade(
            bot_id=bot_id,
            action='TP_HIT',
            symbol=pair,
            price=exit_price,
            amount=total_invested / avg_entry_price if avg_entry_price > 0 else 0,
            cost_usdc=total_invested,
            step=current_step,
            pnl=pnl if calculate_pnl else 0,
            notes=f"TP hit at step {current_step}, avg entry {avg_entry_price:.4f}, exit price {exit_price:.4f}"
        )
        logger.info(f"✅ TP_HIT logged to trade_history for {bot_name} (PnL: ${pnl:.2f})")

        # Reset the trade state
        cursor.execute('''
            UPDATE trades
            SET current_step = 0,
                total_invested = 0,
                avg_entry_price = 0,
                target_tp_price = 0,
                last_exit_price = ?,
                last_exit_time = ?
            WHERE bot_id = ?
        ''', (exit_price, int(time.time()), bot_id))

        conn.commit()
        logger.info(f"✅ Trade state reset for {bot_name} at exit price {exit_price:.4f}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise
    # Note: No conn.close() - using thread-local connection
