import time
import logging
import json
import pandas as pd
import ccxt
import threading
import os
import sys

# Local imports
from engine.exchange_interface import ExchangeInterface
from engine.database import get_bot_status, update_martingale_step, log_trade, reset_bot_after_tp, deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import manage_trade
from engine.bot_management import check_and_execute_stops
from engine.ownership import claim_ownership, check_first_claim_policy
from config.settings import config
from config.constants import (
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
)

# Configure logger specific to this file
logger = logging.getLogger("BotExecutor")

# Thread-local storage for ExchangeInterface instances
# This ensures each thread in the ThreadPoolExecutor gets its own exchange connection
# preventing race conditions on CCXT's internal state (nonce, request signing, etc.)
thread_local_storage = threading.local()

def get_thread_exchange(market_type='future'):
    """
    Get or create a thread-local ExchangeInterface instance.
    Prevents CCXT concurrency issues.
    """
    if not hasattr(thread_local_storage, "exchanges"):
        thread_local_storage.exchanges = {}
    
    if market_type not in thread_local_storage.exchanges:
        # Create new instance for this thread
        # Note: This triggers fetch_markets/inject_markets on first use per thread
        try:
             # Ensure ExchangeInterface is imported (it is, above)
             thread_local_storage.exchanges[market_type] = ExchangeInterface(market_type=market_type)
        except Exception as e:
             logger.error(f"Failed to initialize exchange interface for {market_type} in thread: {e}")
             return None # Match runner.py logic for failed exchange creation if applicable
        
    return thread_local_storage.exchanges[market_type]


class BotExecutor:
    def __init__(self, runner):
        # runner is the BotRunner instance, which holds shared state and exchange objects
        self.runner = runner

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
            
            # USE THREAD-LOCAL EXCHANGE INSTANCE
            # Do NOT use self.exchanges in threads!
            bot_exchange = get_thread_exchange(market_type)
            if not bot_exchange: return

            if not is_active:
                if bot_id in self.runner.strategies:
                    logger.info(f"Bot {name} deactivated. Cleaning up...")
                    bot_exchange.cancel_all_orders(pair)
                    del self.runner.strategies[bot_id]
                return

            if bot_id not in self.runner.strategies:
                if strat_type in ['MQL4', 'Martingale']:
                    self.runner.strategies[bot_id] = MartingaleStrategy(name=name, params=params)
                elif strat_type == 'MarketMaker':
                    from engine.strategies.market_maker import MarketMakerStrategy
                    self.runner.strategies[bot_id] = MarketMakerStrategy(name=name, params=params)
                elif strat_type == 'MagicHour':
                    from engine.strategies.magic_hour_strategy import MagicHourStrategy
                    self.runner.strategies[bot_id] = MagicHourStrategy(name=name, params=params)
                else: return
            
            strategy = self.runner.strategies[bot_id]
            
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
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])  # type: ignore[arg-type]
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            if strat_type == 'MarketMaker':
                # Pass thread-local exchange to MM logic
                self.process_market_maker(bot_id, name, pair, strategy, df, exchange=bot_exchange)
                return

            trade_data = get_bot_status(bot_id)
            if not trade_data or len(trade_data) < 8: return
            
            # DB indices: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time)
            is_in_trade = trade_data[3] > 0
            current_price = df['close'].iloc[-1]
            
            # --- SELF-HEALING: Verify State Sync (Fix for Ghost Trades) ---
            if is_in_trade:
                 # Check if this is a "Zombie" state (DB says Trade, Exchange says Empty)
                 if not self.verify_state_sync(bot_id, name, pair, bot_exchange):
                      is_in_trade = False
                      logger.warning(f"🩹 Bot {name} Auto-Healed: state reset to IDLE.")
                      return # Skip this cycle to allow DB update to propagate
            # -------------------------------------------------------------
            
            # --- LOG STATUS ---
            if is_active:
                status_msg = "IN TRADE" if is_in_trade else "Waiting for Signal"
                logger.info(f"Bot {name} ({pair}): {status_msg}")


            if not is_in_trade:
                # Check for pending entry orders (Non-blocking Chase)
                try:
                    open_orders = bot_exchange.fetch_open_orders(pair) or []
                    entry_side = 'buy' if direction == 'LONG' else 'sell'
                    pending = [o for o in open_orders if isinstance(o, dict) and o.get('side') == entry_side and o.get('type') == 'limit']
                    if pending:
                        self.manage_pending_entry(bot_id, name, pair, direction, pending[0], params, bot_exchange)
                        return
                except Exception as e:
                    logger.error(f"Pending check failed for {name}: {e}")

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
                        # First-Claim Policy Disabled for Virtual Positioning
                        # We allow multiple bots to trade the same pair independently
                        self.execute_entry(bot_id, name, pair, 'buy', base_size, exchange=bot_exchange)
                    elif direction == 'SHORT' and sell_signal:
                        # First-Claim Policy Disabled
                        self.execute_entry(bot_id, name, pair, 'sell', base_size, exchange=bot_exchange)
            else:
                # Check stop-after conditions before managing trade
                from engine.bot_management import check_and_execute_stops
                stop_result = check_and_execute_stops(bot_id, exchange_interface=bot_exchange)
                if stop_result and stop_result.get('action') == 'stop_executed':
                    logger.warning(f"🛑 Stop condition triggered for {name}: {stop_result['reason']}")
                    is_in_trade = False  # Position was closed
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
            ex = exchange or self.runner.exchange
            
            trade_data = get_bot_status(bot_id)
            
            if action == 'tp_hit':
                exit_price = mission.get('exit_price')
                qty = mission.get('qty')
                logger.info(f"[TP MISSION] Closing {bot_name} at {exit_price}")
                
                # Reset locked ATR when trade closes
                if bot_id in self.runner.strategies:
                    strategy = self.runner.strategies[bot_id]
                    if hasattr(strategy, 'reset_locked_atr'):
                        strategy.reset_locked_atr()
                
                if config.DRY_RUN: reset_bot_after_tp(bot_id, exit_price=exit_price)
                else:
                    # Use limit order with chase logic for TP - NO market orders unless emergency
                    side = 'sell' if direction == 'LONG' else 'buy'
                    # Skip validation for TP orders - we're closing existing position, MinNotional doesn't apply
                    success, _, _ = self._execute_limit_with_chase(
                        bot_id, bot_name, pair, side, qty, 
                        exchange=ex, initial_price=exit_price,
                        params={'reduceOnly': True},
                        skip_validation=True  # TP orders bypass MinNotional check
                    )
                    if success:
                        reset_bot_after_tp(bot_id, exit_price=exit_price)
                        logger.info(f"TP Limit Order Filled for {bot_name}")

            elif action == 'maintain_orders':
                grid_price = mission.get('grid_price')
                grid_qty = mission.get('grid_qty')
                grid_step = mission.get('grid_step')
                tp_price = mission.get('tp_price')
                tp_qty = mission.get('tp_qty')
                
                open_orders = ex.fetch_open_orders(pair) or []
                grid_side = 'buy' if direction == 'LONG' else 'sell'
                tp_side = 'sell' if direction == 'LONG' else 'buy'
                
                # =========== MULTI-BOT FIX (v0.5.1) ===========
                # Each bot must check for ITS OWN orders, not just any order on the pair
                # Otherwise, when 5 bots trade the same pair, only 1 gets TP/Grid orders
                bot_order_ids = get_bot_order_ids(bot_id)
                my_tp_order_id = bot_order_ids.get('tp_order_id')
                my_grid_order_ids = [o.get('order_id') for o in bot_order_ids.get('grid_orders', []) if o.get('type') == 'grid']
                
                # Filter exchange orders to only THIS bot's orders
                my_grid_orders = [o for o in open_orders if isinstance(o, dict) and o.get('id') in my_grid_order_ids]
                my_tp_orders = [o for o in open_orders if isinstance(o, dict) and o.get('id') == my_tp_order_id]
                
                logger.debug(f"Maintain {bot_name}: Found {len(my_grid_orders)} Grid, {len(my_tp_orders)} TP orders. Target Grid: {grid_price}, Target TP: {tp_price}")

                # Fallback: If we don't have order ID tracking yet, use side-based matching (legacy)
                # This handles bots that were in trade before the fix
                if not my_tp_order_id and not my_grid_order_ids:
                    # Strict Ownership Check for Legacy Matching
                    can_enter, owner_id, _ = check_first_claim_policy(bot_id, pair)
                    
                    if can_enter:
                        # We are the owner (or first claimant), so we might own these untracked orders
                        logger.warning(f"No order IDs tracked for {bot_name}. Using legacy side-based matching.")
                        my_grid_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == grid_side]
                        my_tp_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == tp_side]
                    else:
                        # We are a PASSENGER. We do NOT own any legacy orders.
                        # The owner bot is responsible for them.
                        logger.info(f"👀 {bot_name}: Passenger on {pair}. Skipping legacy order matching (Owner: Bot {owner_id})")
                        my_grid_orders = []
                        my_tp_orders = []
                # =========== END MULTI-BOT FIX ===========
                
                # --- INDEPENDENT GRID MANAGEMENT ---
                try:
                    grid_ok = False
                    if grid_price and grid_price > 0:
                        for o in my_grid_orders:
                            if abs(float(o['price']) - grid_price) / grid_price < 0.001: grid_ok = True
                            else: ex.exchange.cancel_order(o['id'], pair)
                        if not grid_ok:
                            logger.info(f"[GRID] Placing Limit Grid Order for {bot_name}: {grid_qty:.4f} @ {grid_price}")
                            if config.DRY_RUN:
                                log_trade(bot_id, 'DRY_GRID', pair, grid_price, grid_qty, grid_qty*grid_price, "DRY_GRID", trade_data[2]+1 if trade_data else 0, 0, f"[DRY] Grid {bot_name}")
                            else:
                                # Validate Grid Order
                                is_valid, s_amt, s_price, err = ex.validate_order(pair, grid_side, grid_qty, grid_price)
                                
                                if not is_valid:
                                    logger.error(f"GRID VALIDATION FAILED for {bot_name}: {err}. (Req: {grid_qty:.6f} @ {grid_price:.4f})")
                                else:
                                    logger.info(f"[GRID] Validated OK. Placing {grid_side} {s_amt} @ {s_price} (PostOnly)")
                                    # Added metadata for better tracking (clientOrderId removed to fix Binance -1104)
                                    grid_params = {'postOnly': True}
                                    try:
                                        grid_order = ex.create_order(pair, 'limit', grid_side, s_amt, s_price, params=grid_params)
                                        if grid_order:
                                            logger.info(f"✅ Grid Order Placed: {grid_order.get('id')}")
                                            # Save grid order ID for multi-bot tracking
                                            save_bot_order(bot_id, 'grid', grid_order.get('id'), s_price, s_amt, grid_step if grid_step else 0)
                                        else:
                                            logger.warning(f"⚠️ Grid Order creation returned None for {bot_name}")
                                    except ccxt.InvalidOrder as e:
                                        # Handle Post Only rejection (-5022)
                                        if "-5022" in str(e):
                                            logger.warning(f"Post Only Rejected for {bot_name} at {s_price}. Market moved. Skipping immediate retry.")
                                        else: 
                                            logger.error(f"Grid InvalidOrder: {e}")
                                            raise e
                                    except Exception as e:
                                        logger.error(f"Grid Creation Error: {e}")
                                        raise e
                except Exception as e:
                    logger.error(f"GRID MAINTENANCE FAILED for {bot_name}: {e}")
                
                # --- INDEPENDENT TP MANAGEMENT ---
                try:
                    tp_ok = False
                    if tp_price and tp_price > 0:
                        for o in my_tp_orders:
                            if abs(float(o['price']) - tp_price) / tp_price < 0.001: tp_ok = True
                            else: ex.exchange.cancel_order(o['id'], pair)
                        if not tp_ok:
                            logger.info(f"[TP] Placing Limit TP Order for {bot_name}: {tp_qty:.4f} @ {tp_price}")
                            if config.DRY_RUN:
                                log_trade(bot_id, 'DRY_TP', pair, tp_price, tp_qty, tp_qty*tp_price, "DRY_TP", trade_data[2] if trade_data else 0, 0, f"[DRY] TP {bot_name}")
                            else:
                                # Check if exchange actually has a position before placing TP
                                try:
                                    positions = ex.exchange.fetch_positions()
                                    # FIX: Normalize symbol comparison to handle 'BTC/USDC:USDC' vs 'BTC/USDC'
                                    target_pair = pair.replace('/', '').split(':')[0]
                                    has_position = False
                                    for p in positions:
                                        if not p: continue
                                        pos_symbol = p.get('symbol', '').replace('/', '').split(':')[0]
                                        if pos_symbol == target_pair:
                                            size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                            if size != 0:
                                                has_position = True
                                                break
                                except Exception as pos_err:
                                    logger.warning(f"Could not fetch positions for {bot_name}: {pos_err}")
                                    has_position = True  # Assume position exists if we can't check
                                
                                if not has_position:
                                    logger.warning(f"[TP] Skipping TP order for {bot_name} - no position on exchange (may have already closed)")
                                    return
                                
                                # Use reduceOnly for safety - prevents opening new positions
                                # clientOrderId removed to fix Binance -1104
                                tp_params = {
                                    'reduceOnly': True
                                }
                                tp_order = ex.create_order(pair, 'limit', tp_side, tp_qty, tp_price, params=tp_params)
                                if tp_order:
                                    # Save TP order ID for multi-bot tracking
                                    save_bot_order(bot_id, 'tp', tp_order.get('id'), tp_price, tp_qty, trade_data[2] if trade_data else 0)
                except Exception as e:
                    logger.error(f"TP MAINTENANCE FAILED for {bot_name}: {e}")

            elif action == 'hedge_open':
                price, qty, amount_usd, step = mission.get('price'), mission.get('qty'), mission.get('amount_usd'), mission.get('step')
                side = 'sell' if direction == 'LONG' else 'buy'
                logger.info(f"[HEDGE] Opening Hedge for {bot_name} at {price}")
                if config.DRY_RUN: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, "DRY_HEDGE", step, 0, "[DRY] Hedge")
                else:
                    order = ex.create_order(pair, 'market', side, qty)
                    if order: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, order.get('id'), step, 0, "Hedge Opened")

        except Exception as e: logger.error(f"Mission failed for {mission.get('bot_name')}: {e}")

    def manage_pending_entry(self, bot_id, name, pair, direction, order, params, ex):
        """Manages a pending entry order (chase logic across cycles)."""
        if config.DRY_RUN:
            return

        filled = float(order.get('filled', 0.0))
        remaining = float(order.get('amount', 0.0)) - filled
        
        timestamp = order.get('timestamp') or (time.time() * 1000)
        age_seconds = (time.time() * 1000 - timestamp) / 1000
        
        # Chase interval
        chase_interval = 20
        
        if age_seconds > chase_interval:
            logger.info(f"Entry order {order['id']} timed out ({age_seconds:.1f}s). Chasing...")
            try:
                ex.exchange.cancel_order(order['id'], pair)
                
                # Check for partial fill after cancel
                final_order = ex.fetch_order(order['id'], pair)
                if final_order:
                    filled = float(final_order.get('filled', 0.0))
                
                current_price = ex.get_last_price(pair)
                
                side = 'buy' if direction == 'LONG' else 'sell'
                
                if filled > 0:
                    # Finalize what we have
                    logger.info(f"Partial fill confirmed ({filled}). Finalizing entry.")
                    # Calculate cost roughly or use average price
                    avg_price = float(final_order.get('average', 0.0) or current_price)
                    self._finalize_entry(bot_id, name, pair, side, filled * avg_price, avg_price, order['id'])
                else:
                    # Retry full amount
                    base_size = params.get('base_size', 10.0)
                    self.execute_entry(bot_id, name, pair, side, base_size, exchange=ex)
                    
            except Exception as e:
                logger.error(f"Failed to chase entry for {name}: {e}")

    def process_market_maker(self, bot_id, name, pair, strategy, df, exchange=None):
        try:
            current_price = df['close'].iloc[-1]
            trade_data = get_bot_status(bot_id)
            current_inventory = trade_data[3] if trade_data else 0.0
            ideal_bid, ideal_ask = strategy.calculate_quotes(current_price, current_inventory)
            
            params_raw = get_bot_params(bot_id)
            if not params_raw: return
            params = json.loads(params_raw[7]) if params_raw[7] else {} # config is index 7 in get_bot_params
            mt = params.get('market_type', config.MARKET_TYPE)
            
            # Use passed exchange or get thread-local one
            ex = exchange or get_thread_exchange(mt)
            if not ex: return # Safety check for failed exchange init
            
            open_orders = ex.fetch_open_orders(pair) or []
            current_bids = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'buy']
            current_asks = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'sell']
            
            if not current_bids: self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)
            else:
                best_bid = max(current_bids, key=lambda x: float(x.get('price', 0)))
                bid_price = float(best_bid.get('price', 0)) if best_bid else 0.0
                if bid_price > 0 and abs(bid_price - ideal_bid) / ideal_bid > strategy.reprice_threshold:
                    ex.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)

            if not current_asks: self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
            else:
                best_ask = min(current_asks, key=lambda x: float(x.get('price', 0)))
                ask_price = float(best_ask.get('price', 0)) if best_ask else 0.0
                if ask_price > 0 and abs(ask_price - ideal_ask) / ideal_ask > strategy.reprice_threshold:
                    ex.cancel_all_orders(pair)
                    self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
        except Exception as e: logger.error(f"MM Loop failed for {name}: {e}")

    def _check_order_limits(self, bot_id, name):
        current_day = time.strftime("%Y-%m-%d")
        
        # Accessing state on the runner instance, including the dynamically set _last_reset_day
        if not hasattr(self.runner, '_last_reset_day') or self.runner._last_reset_day != current_day:
            self.runner.orders_today, self.runner._last_reset_day = {}, current_day
        
        if self.runner.orders_this_cycle >= MAX_ORDERS_PER_CYCLE: return False, f"Cycle limit ({MAX_ORDERS_PER_CYCLE})"
        bot_count = self.runner.orders_today.get(bot_id, 0)
        if bot_count >= MAX_ORDERS_PER_BOT_DAILY: return False, f"Daily limit ({bot_count})"
        return True, ""

    def _record_order(self, bot_id):
        self.runner.orders_this_cycle += 1
        self.runner.orders_today[bot_id] = self.runner.orders_today.get(bot_id, 0) + 1

    def _execute_limit_with_chase(self, bot_id, name, pair, side, qty, exchange=None, timeout=None, params={}, initial_price=None, skip_validation=False):
        """
        Executes a Limit Order (Single Shot, Non-Blocking).
        Returns: (success, fill_price, order_id)
        If not successful immediately, order_id is returned but success is False.
        
        Args:
            skip_validation: If True, skip MinNotional check (for TP/reduceOnly orders closing existing positions)
        """
        ex = exchange or self.runner.exchange
        
        try:
            # 1. Get current ticker price
            ticker = ex._safe_request('fetch_ticker', symbol=pair)
            current_price = 0.0
            if ticker:
                val = ticker.get('bid' if side == 'buy' else 'ask')
                if val is not None: current_price = float(val)
            
            # Fallback to initial_price if fetch failed
            if current_price == 0 and initial_price:
                logger.warning(f"Fetch ticker failed for {name}, using initial price {initial_price}")
                current_price = initial_price
            
            if current_price == 0: 
                logger.error(f"Price fetch failed for {name} inside chase.")
                return False, 0.0, None
            
            # 2. Validate (unless skip_validation for TP orders)
            if skip_validation:
                # For TP orders closing existing positions, just sanitize precision
                try:
                    s_amt = float(ex.exchange.amount_to_precision(pair, qty))
                    s_price = float(ex.exchange.price_to_precision(pair, current_price))
                except Exception as e:
                    logger.warning(f"Precision formatting failed for {name}: {e}")
                    s_amt, s_price = qty, current_price
            else:
                is_valid, s_amt, s_price, err = ex.validate_order(pair, side, qty, current_price)
                if not is_valid:
                    logger.error(f"Validation failed for {name}: {err}")
                    return False, 0.0, None

            # 3. Place Limit Order
            # If skip_validation, also tell create_order to skip its validation
            if skip_validation:
                params = dict(params) if params else {}
                params['_skip_validation'] = True
            order = ex.create_order(pair, 'limit', side, s_amt, s_price, params=params)
            if not order: 
                logger.error(f"Order creation returned None for {name}. Check Exchange logs.")
                return False, 0.0, None
                
            last_order_id = order.get('id')
            
            # 4. Wait briefly for immediate fill
            final_order = ex.wait_for_fill(order, timeout_seconds=5)
            
            if final_order:
                fill_avg = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
                filled_fully = final_order.get('status') in ['closed', 'filled']
                if filled_fully:
                    return True, fill_avg, last_order_id
            
            # Not filled immediately -> Return Pending
            return False, 0.0, last_order_id
            
        except Exception as e:
            logger.error(f"Entry attempt failed for {name}: {e}")
            return False, 0.0, None

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params={}, exchange=None):
        can_order, reason = self._check_order_limits(bot_id, name)
        if not can_order:
            logger.critical(f"ORDER BLOCKED for {name}: {reason}. PAUSING BOT.")
            from engine.database import deactivate_bot
            deactivate_bot(bot_id, reason=f"Limit Reached: {reason}")
            return
        
        ex = exchange or self.runner.exchange
        logger.info(f"[ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        if price is None: price = ex.get_last_price(pair)
        if price is None or price <= 0: 
            logger.error(f"Could not get price for {pair}")
            return

        # Validate against Minimum Order Size
        try:
            min_usd = ex.get_min_order_usd(pair, price)
            if min_usd > 0 and amount < min_usd:
                reason = f"Order Size ${amount:.2f} < Min ${min_usd:.2f}"
                logger.error(f"[BLOCK] {reason}. PAUSING BOT.")
                from engine.database import deactivate_bot
                deactivate_bot(bot_id, reason=reason)
                return # Block execution
        except Exception as e:
            logger.error(f"Error checking min size: {e}")

        if config.DRY_RUN:
            self._simulate_dry_run_entry(bot_id, name, pair, side, amount, price)
            return

        # Calculate Qty based on initial price
        qty = amount / price
        
        # Execute using Chase Logic (No Market Fallback)
        # Smart Chase: 60s -> 30s -> 10s -> 5s (Loop)
        # Always Maker (Best Bid/Ask)
        
        success = False
        fill_price = 0.0
        order_id = None
        
        chase_intervals = [60, 30, 10, 5] # Seconds
        interval_idx = 0
        
        while not success:
             current_timeout = chase_intervals[min(interval_idx, len(chase_intervals)-1)]
             
             # Attempt to place order at BEST PRICE (Maker)
             # _execute_limit_with_chase places one order and waits 5s usually. 
             # We need to change that behavior or use it differently.
             # Actually, _execute_limit_with_chase inside calls create_order then wait_for_fill(5s).
             # We want to wait 'current_timeout'. 
             
             # Let's call a new specialized internal method or modify loop here.
             # Logic:
             # 1. Get Best Bid/Ask
             # 2. Place Order
             # 3. Wait 'current_timeout'
             # 4. If not filled, Cancel.
             # 5. Repeat.
             
             ex = exchange or self.runner.exchange
             ticker = ex.fetch_ticker(pair)
             if not ticker: 
                  time.sleep(5)
                  continue
                  
             best_bid = ticker.get('bid')
             best_ask = ticker.get('ask')

             # Fallback: If ticker has no bid/ask (common on some endpoints), fetch Order Book
             if best_bid is None or best_ask is None:
                 try:
                     logger.warning(f"Ticker missing Bid/Ask. Fetching Order Book for {pair}...")
                     book = ex.exchange.fetch_order_book(pair, limit=5)
                     bids = book.get('bids', [])
                     asks = book.get('asks', [])
                     if bids: best_bid = float(bids[0][0])
                     if asks: best_ask = float(asks[0][0])
                 except Exception as e:
                     logger.warning(f"Order Book fetch failed: {e}")
             
             # MAKER LOGIC:
             # Buy: Price = Best Bid. (If we use Best Ask, we are Taker).
             # Sell: Price = Best Ask.
             target_price = best_bid if side == 'buy' else best_ask
             
             if target_price is None or target_price <= 0:
                 logger.warning(f"Sort Chase: Invalid price (Bid:{best_bid}, Ask:{best_ask}). Retrying...")
                 time.sleep(1)
                 continue

             # SAFETY: Ensure we don't cross spread if spread is tight or data is old
             # (CCXT fetch_ticker usually returns current snapshot)
             
             # Recalculate Qty for new price
             qty = amount / target_price
             
             # CRITICAL: Validate notional with ACTUAL order price (final safety check)
             calculated_notional = qty * target_price
            
             # Use safe min size for check (handles step size rounding)
             # This returns the SAFE USD amount (e.g. $178) that is valid
             safe_min_usd = ex.calculate_safe_min_size(pair, target_price)
             
             if safe_min_usd > 0 and calculated_notional < safe_min_usd:
                 reason = f"Notional ${calculated_notional:.2f} < Safe Min ${safe_min_usd:.2f}"
                 logger.error(f"[BLOCK] {reason}. PAUSING BOT.")
                 from engine.database import deactivate_bot
                 deactivate_bot(bot_id, reason=reason)
                 return
             
             logger.info(f"Smart Chase ({current_timeout}s): Placing {side} {qty:.4f} @ {target_price}")
             
             # Place Order
             # Use postOnly to guarantee Maker fees
             order_params = params.copy()
             order_params['postOnly'] = True
             
             try:
                 order = ex.create_order(pair, 'limit', side, qty, target_price, params=order_params)
                 if order:
                     order_id = order['id']
                     
                     # Wait for fill
                     try:
                         final_order = ex.wait_for_fill(order, timeout_seconds=current_timeout)
                         status = final_order.get('status') if final_order else 'unknown'
                         
                         if status in ['closed', 'filled']:
                             fill_price = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
                             success = True
                             break
                         else:
                             # Timeout or Open -> CANCEL
                             logger.info(f"Chase timeout ({current_timeout}s). Status: {status}. Cancelling...")
                             try:
                                 ex.exchange.cancel_order(order_id, pair)
                             except Exception as cancel_err:
                                 logger.warning(f"Cancel failed: {cancel_err}")
                             
                             # Final status check
                             try:
                                 final_check = ex.fetch_order(order_id, pair)
                                 if final_check and final_check.get('status') == 'filled':
                                     fill_price = float(final_check.get('average', 0.0))
                                     success = True
                                     break
                             except:
                                 pass
                                 
                     except Exception as e:
                         logger.error(f"Error waiting for fill: {e}")
                         try: ex.exchange.cancel_order(order_id, pair)
                         except: pass
                     
             except Exception as e:
                 logger.error(f"Chase attempt failed: {e}")
                 time.sleep(5) # Penalty wait
             
             interval_idx += 1
        
        if success:
            self._finalize_entry(bot_id, name, pair, side, amount, fill_price, order_id)

    def _finalize_entry(self, bot_id, name, pair, side, amount, fill_price, order_id):
        tp_price = fill_price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, fill_price, tp_price)
        self._record_order(bot_id)
        # Save entry order ID for multi-bot tracking
        save_bot_order(bot_id, 'entry', order_id, fill_price, amount/fill_price, step=0)
        log_trade(bot_id, 'BUY' if side == 'buy' else 'SELL', pair, fill_price, amount/fill_price, amount, order_id, 0, 0, f"Entry {name}")
        
        # Claim ownership or become passenger based on First-Claim Policy
        success, message = claim_ownership(
            bot_id=bot_id,
            bot_name=name,
            pair=pair,
            entry_order_id=order_id,
            entry_price=fill_price,
            amount_usd=amount,
            tp_price=tp_price
        )
        logger.info(f"🏁 Entry finalized for {name}: {message}")

    def _simulate_dry_run_entry(self, bot_id, name, pair, side, amount, price):
        tp_price = price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, price, tp_price)
        self._record_order(bot_id)
        log_trade(bot_id, 'DRY_BUY' if side == 'buy' else 'DRY_SELL', pair, price, amount/price, amount, "DRY_RUN", 0, 0, f"Dry Entry {name}")

    def verify_state_sync(self, bot_id, name, pair, exchange):
        """
        Robustness Check: Detect 'Ghost Trades' where DB says In Trade, but Exchange is Empty.
        Returns: True if state is VALID (or corrected), False if state was INVALID and reset.
        """
        if config.DRY_RUN: return True # Skip for Dry Run

        try:
            # 1. Fetch Positions (Target Pair Only)
            # Fetching all positions is safer/standard in CCXT for Futures
            positions = exchange.fetch_positions() 
            has_position = False
            
            # Normalize pair for comparison (BTC/USDT:USDT -> BTCUSDT etc)
            target_clean = pair.replace('/', '').split(':')[0]
            
            for p in positions:
                if not p: continue
                # Symbol matching logic
                p_sym = p.get('symbol', '').replace('/', '').split(':')[0]
                if p_sym == target_clean:
                    size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if size != 0:
                        has_position = True
                        break
            
            # 2. Fetch Open Orders
            open_orders = exchange.fetch_open_orders(pair) or []
            has_orders = len(open_orders) > 0
            
            # 3. Decision Logic
            if not has_position and not has_orders:
                # CRITICAL: DB says Trade, Exchange says NOTHING.
                logger.critical(f"👻 GHOST TRADE DETECTED for {name} ({pair})! DB In-Trade vs Empty Wallet. Auto-Healing...")
                
                # Force Reset DB State
                # Using 0 as exit price since no real trade exists
                reset_bot_after_tp(bot_id, exit_price=0)
                
                return False # State was invalid and reset
            
            return True # State is valid (or at least has exchange presence)
            
        except Exception as e:
            logger.error(f"State Sync Check Failed for {name}: {e}")
            return True # Fail open (assume valid) to prevent accidental resets during API errors
