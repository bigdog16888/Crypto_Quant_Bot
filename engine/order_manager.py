import time
import logging
from .exchange_interface import ExchangeInterface

class OrderManager:
    def __init__(self, exchange: ExchangeInterface):
        self.exchange = exchange
        self.logger = logging.getLogger(__name__)

    def chase_limit_order(self, symbol, side, amount, max_retries=5, chase_step_pct=0.001):
        """
        Implements basic 'order chasing' logic.
        If the order isn't filled immediately, cancel and replace at a better price.
        """
        self.logger.info(f"Starting limit chase for {side} {amount} {symbol}")
        
        # Get current ticker
        ticker = self.exchange.exchange.fetch_ticker(symbol)
        price = ticker['ask'] if side == 'buy' else ticker['bid']
        
        order = self.exchange.create_order(symbol, 'limit', side, amount, price)
        
        if order.get('status') == 'dry_run':
            return order

        retries = 0
        while retries < max_retries:
            time.sleep(5) # Wait for fill
            
            order_status = self.exchange.exchange.fetch_order(order['id'], symbol)
            if order_status['status'] == 'closed':
                self.logger.info("Order filled completely.")
                return order_status
            
            # Not filled, cancel and move
            self.logger.info(f"Order not filled. Retry {retries + 1}/{max_retries}. Chasing...")
            self.exchange.exchange.cancel_order(order['id'], symbol)
            
            ticker = self.exchange.exchange.fetch_ticker(symbol)
            price = ticker['ask'] if side == 'buy' else ticker['bid']
            
            # Apply offset if needed or just track spread
            order = self.exchange.create_order(symbol, 'limit', side, amount, price)
            retries += 1
            
        return order
