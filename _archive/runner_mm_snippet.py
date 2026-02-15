    def process_market_maker(self, bot_id, name, pair, strategy, df):
        """
        Executes the specific loop for Market Making bots.
        """
        try:
            current_price = df['close'].iloc[-1]
            
            # 1. Get Inventory
            # In a real scenario, fetch from Exchange. For v0.4, use DB state or mock.
            # Here we assume 'total_invested' in DB reflects net position (signed).
            trade_data = get_bot_status(bot_id)
            # trade_data: (name, pair, current_step, total_invested, avg_price, tp_price)
            current_inventory = trade_data[3] if trade_data else 0.0
            
            # 2. Calculate Quotes
            ideal_bid, ideal_ask = strategy.calculate_quotes(current_price, current_inventory)
            
            # 3. Reconcile (Update Orders)
            # Fetch open orders
            open_orders = self.exchange.fetch_open_orders(pair)
            
            # Separate Bid/Ask
            current_bids = [o for o in open_orders if o['side'] == 'buy']
            current_asks = [o for o in open_orders if o['side'] == 'sell']
            
            # --- Bid Logic ---
            if not current_bids:
                self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True})
            else:
                best_bid = max(current_bids, key=lambda x: x['price'])
                bid_price = float(best_bid['price'])
                
                # Check deviation
                diff = abs(bid_price - ideal_bid) / ideal_bid
                if diff > strategy.reprice_threshold:
                    logger.info(f"MM {name}: Repricing Bid. Old: {bid_price}, New: {ideal_bid}")
                    self.exchange.cancel_all_orders(pair) # Simple cancel all for now
                    self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True})

            # --- Ask Logic ---
            if not current_asks:
                self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True})
            else:
                best_ask = min(current_asks, key=lambda x: x['price'])
                ask_price = float(best_ask['price'])
                
                diff = abs(ask_price - ideal_ask) / ideal_ask
                if diff > strategy.reprice_threshold:
                    logger.info(f"MM {name}: Repricing Ask. Old: {ask_price}, New: {ideal_ask}")
                    self.exchange.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True})

        except Exception as e:
            logger.error(f"MM Loop failed for {name}: {e}")