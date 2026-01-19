    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params={}):
        """
        Place the first order and initialize the trade in DB.
        """
        logger.info(f"[ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        # Validated Create Order
        # Fetch current price for limit order safety
        if price is None:
            price = self.exchange.get_last_price(pair)
        
        if price == 0:
            logger.error(f"Could not fetch price for {pair}, aborting entry.")
            return

        # Sanity check direction vs side
        # side is 'buy' or 'sell'
        
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Simulating entry for {name} at {price}")
            tp_price = price * (1.01 if side == 'buy' else 0.99)
            update_martingale_step(bot_id, 0, amount, price, tp_price)
        else:
            # Real Order
            try:
                # Use create_order which now has validation and retries
                order = self.exchange.create_order(pair, 'limit', side, amount, price, params=params)
                if order:
                    logger.info(f"Order placed: {order.get('id')}")
                    # Update DB only if successful
                    tp_price = price * (1.01 if side == 'buy' else 0.99) # Initial TP assumption
                    update_martingale_step(bot_id, 0, amount, price, tp_price)
            except Exception as e:
                logger.error(f"Entry failed for {name}: {e}")

    def run_cycle(self):