import time
import logging
import json
import sys
import os
import pandas as pd
import ccxt

# Add root to sys.path to ensure module resolution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, init_db, get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids
from engine.exchange_interface import ExchangeInterface
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.sync import sync_bot_state
from config.settings import config
from config.constants import (
    MIN_ORDER_USD,
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
    POLL_INTERVAL_SECONDS,
    ORDER_FILL_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    STABLECOINS
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.PATHS["LOG_FILE"]),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BotRunner")

class BotRunner:
    def __init__(self):
        self.running = False
        # Manage multiple exchange instances for different market types
        self.exchanges = {
            'spot': ExchangeInterface(market_type='spot'),
            'future': ExchangeInterface(market_type='future')
        }
        # For backward compatibility and global actions
        self.exchange = self.exchanges.get(config.MARKET_TYPE, self.exchanges['future'])
        self.strategies = {} # Cache strategy instances: {bot_id: strategy_instance}
        
        # Safety / Circuit Breaker State
        self.initial_equity = 0.0
        self.circuit_breaker_triggered = False
        
        # ========== RUNAWAY ORDER PROTECTION ==========
        self.orders_this_cycle = 0
        self.orders_today = {}  # {bot_id: count}
        self.last_order_reset = time.time()
        
        self._initialize_safety_baseline()
        
        # State Synchronization
        try:
            self.sync_all_bots()
        except Exception as e:
            logger.error(f"Failed to sync bots on startup (non-fatal): {e}")

    def _calculate_stablecoin_balance(self, balance: dict) -> float:
        """Calculate total balance across USDT and USDC stablecoins."""
        total = 0.0
        for currency in STABLECOINS:
            curr_bal = balance.get(currency)
            if isinstance(curr_bal, dict):
                total += float(curr_bal.get('total', 0.0))
        return total

    def sync_all_bots(self):
        """
        Synchronizes the state of all active bots with the exchange on startup.
        """
        logger.info("Starting global state synchronization...")
        active_bots = self.get_active_bots()
        for bot in active_bots:
            # bot tuple: (id, name, pair, direction, strategy_type, config, base_size, mm, rsi, is_active)
            bot_id = bot[0]
            config_json = bot[5] # Corrected index for config
            config_dict = json.loads(config_json) if config_json else {}
            market_type = config_dict.get('market_type', config.MARKET_TYPE)
            bot_exchange = self.exchanges.get(market_type, self.exchange)
            sync_bot_state(bot_id, bot_exchange)
        logger.info("Global state synchronization complete.")


    def _initialize_safety_baseline(self):
        """Captures initial account state for Drawdown monitoring."""
        try:
            total_stablecoin = 0.0
            # Get only bots marked as active
            active_bots = [b for b in self.get_active_bots() if b[9] == 1]
            
            # Determine which market types are actually active
            active_market_types = set()
            for bot in active_bots:
                # bot tuple: (id, name, pair, direction, strategy_type, config, base_size, mm, rsi_limit, is_active)
                config_json = bot[5] # Index 5
                config_dict = json.loads(config_json) if config_json else {}
                active_market_types.add(config_dict.get('market_type', config.MARKET_TYPE))
            
            # Always include global default if no bots active
            if not active_market_types:
                active_market_types.add(config.MARKET_TYPE)

            # Fetch balances ONLY from active market types
            for mt in active_market_types:
                if mt in self.exchanges:
                    try:
                        ex = self.exchanges[mt]
                        balance = ex.fetch_balance()
                        if balance:
                            total_stablecoin += self._calculate_stablecoin_balance(balance)
                    except Exception as ex_err:
                        logger.warning(f"Failed to fetch {mt} balance for safety baseline: {ex_err}")
                
            invested_sum = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and len(t_data) > 3:
                    invested_sum += float(t_data[3])
            
            self.initial_equity = total_stablecoin + invested_sum
            logger.info(f"Safety Baseline Initialized. Equity: ${self.initial_equity:.2f} (Cash: {total_stablecoin:.2f} + Pos: {invested_sum:.2f})")
            
        except Exception as e:
            logger.error(f"Failed to initialize safety baseline: {e}")
            self.initial_equity = 0.0

    def check_circuit_breaker(self):
        """
        Global Circuit Breaker: Checks if account equity has dropped below safe limits.
        """
        if self.circuit_breaker_triggered or self.initial_equity <= 0:
            return

        try:
            # Only check active market types
            active_bots_raw = self.get_active_bots()
            active_bots = [b for b in active_bots_raw if b[9] == 1]
            active_market_types = set()
            for bot in active_bots:
                config_json = bot[5]
                config_dict = json.loads(config_json) if config_json else {}
                active_market_types.add(config_dict.get('market_type', config.MARKET_TYPE))
            
            if not active_market_types:
                active_market_types.add(config.MARKET_TYPE)

            total_stablecoin = 0.0
            for mt in active_market_types:
                if mt in self.exchanges:
                    try:
                        balance = self.exchanges[mt].fetch_balance()
                        if balance:
                            total_stablecoin += self._calculate_stablecoin_balance(balance)
                    except Exception: pass
                
            invested_cost = 0.0
            for bot in active_bots:
                t_data = get_bot_status(bot[0])
                if t_data and len(t_data) > 3 and t_data[3] > 0:
                    invested_cost += float(t_data[3])
            
            current_equity = total_stablecoin + invested_cost
            if self.initial_equity > 0:
                drawdown = (self.initial_equity - current_equity) / self.initial_equity * 100
                if drawdown >= config.GLOBAL_STOP_LOSS_PCT:
                    logger.critical(f"CIRCUIT BREAKER TRIGGERED! Drawdown: {drawdown:.2f}%")
                    self.circuit_breaker_triggered = True
                    with open(config.PATHS["EMERGENCY_FILE"], "w") as f:
                        f.write(f"Circuit Breaker Triggered at {drawdown:.2f}% drawdown")
                    self.handle_emergency_liquidation()
        except Exception as e:
            logger.error(f"Circuit breaker check failed: {e}")

    def get_active_bots(self):
        """Fetches all bots and their current status."""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Query returns all bots
            cursor.execute('''
                SELECT id, name, pair, direction, strategy_type, config, base_size, martingale_multiplier, rsi_limit, is_active
                FROM bots 
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching bots: {e}")
            return []
        finally: conn.close()

    def process_bot(self, bot_data):
        """Main logic loop for a single bot."""
        bot_id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit, is_active = bot_data
        
        try:
            params = json.loads(config_json) if config_json else {}
            params.update({
                'direction': direction, 
                'base_size': base_size, 
                'martingale_multiplier': mm, 
                'rsi_limit': rsi_limit
            })
            timeframe = params.get('timeframe', '1h')
            market_type = params.get('market_type', config.MARKET_TYPE)
            bot_exchange = self.exchanges.get(market_type, self.exchange)

            if not is_active:
                if bot_id in self.strategies:
                    logger.info(f"Bot {name} deactivated. Cleaning up...")
                    bot_exchange.cancel_all_orders(pair)
                    del self.strategies[bot_id]
                return

            if bot_id not in self.strategies:
                if strat_type in ['MQL4', 'Martingale']:
                    self.strategies[bot_id] = MartingaleStrategy(name=name, params=params)
                elif strat_type == 'MarketMaker':
                    from engine.strategies.market_maker import MarketMakerStrategy
                    self.strategies[bot_id] = MarketMakerStrategy(name=name, params=params)
                elif strat_type == 'MagicHour':
                    from engine.strategies.magic_hour_strategy import MagicHourStrategy
                    self.strategies[bot_id] = MagicHourStrategy(name=name, params=params)
                else: return

            strategy = self.strategies[bot_id]
            
            # Leverage
            leverage = params.get('leverage', 1)
            if leverage > 1 and bot_exchange.market_type in ['future', 'swap']:
                if not hasattr(strategy, '_leverage_set') or strategy._leverage_set != leverage:
                    if bot_exchange.set_leverage(pair, leverage):
                        logger.info(f"Bot {name}: Leverage set to {leverage}x")
                        strategy._leverage_set = leverage

            # Market Data
            ohlcv = bot_exchange.fetch_ohlcv(symbol=pair, timeframe=timeframe, limit=100)
            if not ohlcv: return
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            if strat_type == 'MarketMaker':
                self.process_market_maker(bot_id, name, pair, strategy, df)
                return

            trade_data = get_bot_status(bot_id)
            if not trade_data or len(trade_data) < 8: return
            
            # DB indices: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time)
            is_in_trade = trade_data[3] > 0
            current_price = df['close'].iloc[-1]
            
            # --- LOG STATUS ---
            if is_active:
                status_msg = "IN TRADE" if is_in_trade else "SCANNING for Entry"
                logger.info(f"Bot {name} ({pair}): {status_msg}")


            if not is_in_trade:
                last_exit_price = trade_data[6]
                last_exit_time = trade_data[7]
                can_enter = True
                
                reentry_mins = params.get('reentry_cooldown_mins', 0)
                if last_exit_time > 0 and reentry_mins > 0:
                    if (time.time() - last_exit_time) / 60 < reentry_mins: can_enter = False
                
                reentry_dist_pct = params.get('reentry_distance_pct', 0.0)
                if last_exit_price > 0 and reentry_dist_pct > 0:
                    if abs(current_price - last_exit_price) / last_exit_price * 100 < reentry_dist_pct: can_enter = False

                if can_enter:
                    buy_signal, sell_signal = strategy.check_signals(df)
                    if direction == 'LONG' and buy_signal:
                        self.execute_entry(bot_id, name, pair, 'buy', base_size, exchange=bot_exchange)
                    elif direction == 'SHORT' and sell_signal:
                        self.execute_entry(bot_id, name, pair, 'sell', base_size, exchange=bot_exchange)
            else:
                strategy.last_market_data = df
                mission = manage_trade(bot_id, name, pair, direction, params, trade_data, current_price, strategy, bot_exchange)
                if mission and mission.get('action') != 'none':
                    self.execute_mission(mission, exchange=bot_exchange)

        except (ccxt.BadSymbol, ccxt.ExchangeError) as e:
            if "symbol" in str(e).lower() or "market" in str(e).lower():
                logger.error(f"INVALID SYMBOL for bot {name}: {e}. Deactivating.")
                deactivate_bot(bot_id, reason=f"Invalid Symbol: {e}")
            else:
                logger.error(f"Exchange error for bot {name}: {e}")
        except Exception as e:
            logger.error(f"Error processing bot {name}: {e}")

    def execute_mission(self, mission, exchange=None):
        if not mission: return
        try:
            action = mission.get('action')
            bot_id = mission.get('bot_id')
            bot_name = mission.get('bot_name')
            pair = mission.get('pair')
            direction = mission.get('direction')
            ex = exchange or self.exchange
            
            trade_data = get_bot_status(bot_id)
            
            if action == 'tp_hit':
                exit_price = mission.get('exit_price')
                qty = mission.get('qty')
                logger.info(f"[TP MISSION] Closing {bot_name} at {exit_price}")
                if config.DRY_RUN: reset_bot_after_tp(bot_id, exit_price=exit_price)
                else:
                    side = 'sell' if direction == 'LONG' else 'buy'
                    if ex.create_order(pair, 'market', side, qty):
                        reset_bot_after_tp(bot_id, exit_price=exit_price)
                        logger.info(f"TP Market Order Filled for {bot_name}")

            elif action == 'maintain_orders':
                grid_price = mission.get('grid_price')
                grid_qty = mission.get('grid_qty')
                tp_price = mission.get('tp_price')
                tp_qty = mission.get('tp_qty')
                
                open_orders = ex.fetch_open_orders(pair) or []
                grid_side = 'buy' if direction == 'LONG' else 'sell'
                tp_side = 'sell' if direction == 'LONG' else 'buy'
                
                grid_orders = [o for o in open_orders if o and o.get('side') == grid_side]
                tp_orders = [o for o in open_orders if o and o.get('side') == tp_side]
                
                # Manage Grid
                grid_ok = False
                if grid_price and grid_price > 0:
                    for o in grid_orders:
                        if abs(float(o['price']) - grid_price) / grid_price < 0.001: grid_ok = True
                        else: ex.exchange.cancel_order(o['id'], pair)
                    if not grid_ok:
                        logger.info(f"[GRID] Placing Limit Grid Order for {bot_name}: {grid_qty:.4f} @ {grid_price}")
                        if config.DRY_RUN:
                            log_trade(bot_id, 'DRY_GRID', pair, grid_price, grid_qty, grid_qty*grid_price, "DRY_GRID", trade_data[2]+1 if trade_data else 0, 0, f"[DRY] Grid {bot_name}")
                        
                        # Validate Grid Order
                        is_valid, s_amt, s_price, err = ex.validate_order(pair, grid_side, grid_qty, grid_price)
                        if not is_valid:
                            logger.error(f"GRID VALIDATION FAILED for {bot_name}: {err}. Deactivating.")
                            from engine.database import deactivate_bot
                            deactivate_bot(bot_id, reason=f"Grid Validation: {err}")
                            return

                        # Added metadata for better tracking
                        grid_params = {'postOnly': True, 'clientOrderId': f"grid_{bot_id}_{int(time.time())}"}
                        try:
                            ex.create_order(pair, 'limit', grid_side, s_amt, s_price, params=grid_params)
                        except ccxt.InvalidOrder as e:
                            # Handle Post Only rejection (-5022)
                            if "-5022" in str(e):
                                logger.warning(f"Post Only Rejected for {bot_name} at {s_price}. Market moved. Retrying as regular limit...")
                                grid_params.pop('postOnly', None)
                                ex.create_order(pair, 'limit', grid_side, s_amt, s_price, params=grid_params)
                            else: raise e
                
                # Manage TP
                tp_ok = False
                if tp_price and tp_price > 0:
                    for o in tp_orders:
                        if abs(float(o['price']) - tp_price) / tp_price < 0.001: tp_ok = True
                        else: ex.exchange.cancel_order(o['id'], pair)
                    if not tp_ok:
                        logger.info(f"[TP] Placing Limit TP Order for {bot_name}: {tp_qty:.4f} @ {tp_price}")
                        if config.DRY_RUN:
                            log_trade(bot_id, 'DRY_TP', pair, tp_price, tp_qty, tp_qty*tp_price, "DRY_TP", trade_data[2] if trade_data else 0, 0, f"[DRY] TP {bot_name}")
                        
                        # Added metadata for better tracking
                        tp_params = {'reduceOnly': True, 'clientOrderId': f"tp_{bot_id}_{int(time.time())}"}
                        tp_order = ex.create_order(pair, 'limit', tp_side, tp_qty, tp_price, params=tp_params)
                        if tp_order:
                            # Save TP order ID for multi-bot tracking
                            save_bot_order(bot_id, 'tp', tp_order.get('id'), tp_price, tp_qty)

            elif action == 'hedge_open':
                price, qty, amount_usd, step = mission.get('price'), mission.get('qty'), mission.get('amount_usd'), mission.get('step')
                side = 'sell' if direction == 'LONG' else 'buy'
                logger.info(f"[HEDGE] Opening Hedge for {bot_name} at {price}")
                if config.DRY_RUN: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, "DRY_HEDGE", step, 0, "[DRY] Hedge")
                else:
                    order = ex.create_order(pair, 'market', side, qty)
                    if order: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, order.get('id'), step, 0, "Hedge Opened")

        except Exception as e: logger.error(f"Mission failed for {mission.get('bot_name')}: {e}")

    def process_market_maker(self, bot_id, name, pair, strategy, df):
        try:
            current_price = df['close'].iloc[-1]
            trade_data = get_bot_status(bot_id)
            current_inventory = trade_data[3] if trade_data else 0.0
            ideal_bid, ideal_ask = strategy.calculate_quotes(current_price, current_inventory)
            
            params_raw = get_bot_params(bot_id)
            if not params_raw: return
            params = json.loads(params_raw[7]) if params_raw[7] else {} # config is index 7 in get_bot_params
            mt = params.get('market_type', config.MARKET_TYPE)
            ex = self.exchanges.get(mt, self.exchange)
            
            open_orders = ex.fetch_open_orders(pair) or []
            current_bids = [o for o in open_orders if o and o.get('side') == 'buy']
            current_asks = [o for o in open_orders if o and o.get('side') == 'sell']
            
            if not current_bids: self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)
            else:
                bid_price = float(max(current_bids, key=lambda x: float(x.get('price', 0))).get('price', 0))
                if abs(bid_price - ideal_bid) / ideal_bid > strategy.reprice_threshold:
                    ex.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)

            if not current_asks: self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
            else:
                ask_price = float(min(current_asks, key=lambda x: float(x.get('price', 0))).get('price', 0))
                if abs(ask_price - ideal_ask) / ideal_ask > strategy.reprice_threshold:
                    ex.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
        except Exception as e: logger.error(f"MM Loop failed for {name}: {e}")

    def _check_order_limits(self, bot_id, name):
        current_day = time.strftime("%Y-%m-%d")
        if not hasattr(self, '_last_reset_day') or self._last_reset_day != current_day:
            self.orders_today, self._last_reset_day = {}, current_day
        if self.orders_this_cycle >= MAX_ORDERS_PER_CYCLE: return False, f"Cycle limit ({MAX_ORDERS_PER_CYCLE})"
        bot_count = self.orders_today.get(bot_id, 0)
        if bot_count >= MAX_ORDERS_PER_BOT_DAILY: return False, f"Daily limit ({bot_count})"
        return True, ""

    def _record_order(self, bot_id):
        self.orders_this_cycle += 1
        self.orders_today[bot_id] = self.orders_today.get(bot_id, 0) + 1

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params={}, exchange=None):
        can_order, reason = self._check_order_limits(bot_id, name)
        if not can_order:
            logger.critical(f"ORDER BLOCKED for {name}: {reason}")
            return
        
        ex = exchange or self.exchange
        logger.info(f"[ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        if price is None: price = ex.get_last_price(pair)
        if price == 0: return

        if config.DRY_RUN:
            self._simulate_dry_run_entry(bot_id, name, pair, side, amount, price)
            return

        # Fetch intervals from bot config or use default
        bot_params_raw = get_bot_params(bot_id)
        bot_config = json.loads(bot_params_raw[7]) if bot_params_raw and bot_params_raw[7] else {}
        chase_intervals = bot_config.get('chase_intervals', [10, 5, 2])
        
        for interval in chase_intervals:
            try:
                ticker = ex._safe_request('fetch_ticker', symbol=pair)
                if ticker:
                    val = ticker.get('bid' if side == 'buy' else 'ask')
                    if val is not None: price = float(val)
                qty = amount / price
                
                # Check validation before attempt
                is_valid, s_amt, s_price, err = ex.validate_order(pair, side, qty, price)
                if not is_valid:
                    logger.error(f"ENTRY VALIDATION FAILED for {name}: {err}. Deactivating.")
                    from engine.database import deactivate_bot
                    deactivate_bot(bot_id, reason=f"Entry Validation: {err}")
                    return

                order = ex.create_order(pair, 'limit', side, s_amt, s_price, params=params)
                if not order: continue
                filled, final_order = ex.wait_for_fill(order.get('id'), pair, timeout_seconds=interval)
                if filled:
                    self._finalize_entry(bot_id, name, pair, side, amount, final_order.get('average', s_price), order.get('id'))
                    return
                ex.exchange.cancel_order(order.get('id'), pair)
            except Exception as e: logger.error(f"Entry attempt failed: {e}")

        logger.warning(f"Chasing failed for {name}. Fallback to MARKET.")
        try:
            curr_p = ex.get_last_price(pair)
            if curr_p > 0:
                qty = amount / curr_p
                order = ex.create_order(pair, 'market', side, qty)
                if order: self._finalize_entry(bot_id, name, pair, side, amount, curr_p, order.get('id'))
            else:
                logger.error(f"Market fallback failed for {name}: Price is 0")
        except Exception as e: logger.error(f"Market fallback failed: {e}")

    def _finalize_entry(self, bot_id, name, pair, side, amount, fill_price, order_id):
        tp_price = fill_price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, fill_price, tp_price)
        self._record_order(bot_id)
        # Save entry order ID for multi-bot tracking
        save_bot_order(bot_id, 'entry', order_id, fill_price, amount/fill_price)
        log_trade(bot_id, 'BUY' if side == 'buy' else 'SELL', pair, fill_price, amount/fill_price, amount, order_id, 0, 0, f"Entry {name}")

    def _simulate_dry_run_entry(self, bot_id, name, pair, side, amount, price):
        tp_price = price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, price, tp_price)
        self._record_order(bot_id)
        log_trade(bot_id, 'DRY_BUY' if side == 'buy' else 'DRY_SELL', pair, price, amount/price, amount, "DRY_RUN", 0, 0, f"Dry Entry {name}")

    def run_cycle(self):
        self.orders_this_cycle = 0
        self.check_circuit_breaker()
        if os.path.exists(config.PATHS["EMERGENCY_FILE"]):
            self.handle_emergency_liquidation()
            self.running = False
            return False
        if os.path.exists(config.PATHS["STOP_FILE"]):
            self.running = False
            return False

        bots = self.get_active_bots()
        for bot in bots:
            if os.path.exists(config.PATHS["EMERGENCY_FILE"]) or os.path.exists(config.PATHS["STOP_FILE"]): break
            self.process_bot(bot)
        return True

    def handle_emergency_liquidation(self):
        bots = self.get_active_bots()
        for bot in bots:
            # bot tuple: (id, name, pair, direction, strategy_type, config, base_size, mm, rsi_limit, is_active)
            id, name, pair = bot[0], bot[1], bot[2]
            config_json = bot[5] # config is at index 5
            config_dict = json.loads(config_json) if config_json else {}
            ex = self.exchanges.get(config_dict.get('market_type', config.MARKET_TYPE), self.exchange)
            try:
                ex.cancel_all_orders(pair)
                if not config.DRY_RUN:
                    base = pair.split('/')[0]
                    # Simple spot balance fetch for liquidation
                    balance = ex.fetch_balance()
                    qty = balance.get(base, {}).get('free', 0)
                    if qty > 0:
                        logger.warning(f"Emergency Market Close {qty} {base} for {name}")
                        # ex.create_order(pair, 'market', 'sell', qty) # Still commented for safety
            except Exception as e: logger.error(f"Cleanup failed for {name}: {e}")

if __name__ == "__main__":
    init_db()
    logger.info("Bot Service Started.")
    try: runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        sys.exit(1)
    runner.running = True
    PID, STOP = config.PATHS["PID_FILE"], config.PATHS["STOP_FILE"]
    if os.path.exists(STOP): os.remove(STOP)
    with open(PID, "w") as f: f.write(str(os.getpid()))
    failures = 0
    while runner.running:
        try:
            if not runner.run_cycle(): break
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"Cycle failed ({failures}): {e}")
            if failures >= MAX_CONSECUTIVE_FAILURES: break
        time.sleep(POLL_INTERVAL_SECONDS)
    if os.path.exists(PID): os.remove(PID)
