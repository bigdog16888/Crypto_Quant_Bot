import time
import logging
from .exchange_interface import ExchangeInterface

class OrderManager:
    def __init__(self, exchange: ExchangeInterface):
        self.exchange = exchange
        self.logger = logging.getLogger(__name__)

    def chase_limit_order(self, symbol, side, amount, bot_id=None, max_retries=5, chase_step_pct=0.001):
        """
        Implements basic 'order chasing' logic.
        If the order isn't filled immediately, cancel and replace at a better price.
        """
        self.logger.info(f"Starting limit chase for {side} {amount} {symbol} (Bot: {bot_id})")
        
        # Get current ticker using safe wrapper
        price = self.exchange.get_last_price(symbol)
        if price == 0:
            self.logger.error(f"Could not get price for {symbol}")
            return None
        
        order = self.exchange.create_order(symbol, 'limit', side, amount, price, bot_id=bot_id)
        
        if not order:
            return None
            
        if order.get('status') == 'dry_run':
            return order

        retries = 0
        while retries < max_retries:
            time.sleep(5) # Wait for fill
            
            try:
                order_status = self.exchange.fetch_order(order['id'], symbol)
                if order_status and order_status.get('status') == 'closed':
                    self.logger.info("Order filled completely.")
                    return order_status
            except Exception as e:
                self.logger.warning(f"Error fetching order status: {e}")
            
            # Not filled, cancel and move
            self.logger.info(f"Order not filled. Retry {retries + 1}/{max_retries}. Chasing...")
            try:
                if bot_id:
                    self.exchange.cancel_orders_by_bot_id(bot_id, symbol)
                else:
                    self.exchange.cancel_all_orders(symbol)
            except Exception:
                pass
            
            price = self.exchange.get_last_price(symbol)
            if price == 0:
                break
            
            order = self.exchange.create_order(symbol, 'limit', side, amount, price, bot_id=bot_id)
            if not order:
                break
            retries += 1
            
        return order
