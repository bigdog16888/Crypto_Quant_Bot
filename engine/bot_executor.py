import time
import logging
import json
import pandas as pd
import ccxt
import threading
from typing import Dict, Any, Union, Optional

# Local imports (refreshed)
from engine.database import (
    log_trade, get_bot_status, update_martingale_step, reset_bot_after_tp, 
    deactivate_bot, get_bot_params, save_bot_order, get_bot_order_ids, 
    get_last_filled_order, update_bot_display_status
)
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.risk_manager import check_daily_loss_limit
from engine.manager import manage_trade
from engine.bot_management import check_and_execute_stops
from engine.ownership import claim_ownership, check_first_claim_policy
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config
from config.constants import (
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
)
from engine.exceptions import InsufficientFundsError, OrderNotFoundError, APIError, NetworkError

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

    def process_bot(self, bot_data, exchange_snapshot=None):
        """
        Main logic loop for a single bot.
        exchange_snapshot: Optional dict containing pre-fetched snapshots (positions, balance, open_orders)
        """
        bot_id, name, pair, direction, strat_type, config_json, base_size, mm, rsi_limit, is_active = bot_data
        
        # DEBUG TRACE: ENTRY
        try:
            logger.error(f"ENTERING PROCESS_BOT for {name}")
            log_trade(bot_id, 'DEBUG_LOG', pair, 0, 0, 0, "PROC_ENTRY", 0, 0, "Enter process_bot")
        except Exception:
            pass  # Silently fail logging - don't block bot execution
        
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
            
            # --- PHASE 10.2: Risk Management (Daily Loss Limit) ---
            # Check safely (e.g., limit = 50.0 means max $50 loss)
            # We configure this in bot params? Or global system params?
            # For now, let's assume valid param 'daily_loss_limit' in bot config
            limit = float(params.get('daily_loss_limit', 0.0))
            if limit > 0 and check_daily_loss_limit(limit, bot_id):
                 logger.warning(f"🛑 Bot {name} HIT DAILY LOSS LIMIT (${limit}). Skipping processing.")
                 return # Skip this cycle

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
            
            # Leverage - ROBUST UNIVERSAL ENFORCEMENT
            leverage = int(params.get('leverage', 1))
            if leverage > 1 and bot_exchange.market_type in ['future', 'swap']:
                # Always try to set/verify leverage on startup or periodic checks
                try:
                    # 1. Check if we need to Verify
                    should_check = True
                    if hasattr(strategy, '_leverage_verified') and strategy._leverage_verified == leverage:
                        should_check = False
                        
                    if should_check:
                         # 2. Verify Current Real Leverage (Calculated or Explicit)
                         real_lev = bot_exchange.calculate_real_leverage(pair)
                         
                         if real_lev is None or real_lev != leverage:
                              logger.info(f"⚙️ Bot {name}: Leverage Adjustment Needed (Target: {leverage}x, Actual: {real_lev}x)")
                              success = bot_exchange.set_leverage(pair, leverage)
                              if success:
                                  logger.info(f"✅ Bot {name}: Leverage successfully set to {leverage}x")
                                  strategy._leverage_verified = leverage
                              else:
                                  logger.error(f"⚠️ Bot {name}: Failed to set leverage to {leverage}x. Orders may fail.")
                         else:
                              # Already correct
                              if not hasattr(strategy, '_leverage_verified'):
                                   logger.info(f"✅ Bot {name}: Leverage Verified at {real_lev}x")
                              strategy._leverage_verified = leverage

                except Exception as lev_err:
                     logger.error(f"❌ Bot {name}: Exception setting leverage: {lev_err}")
                     
            # Market Data
            ohlcv = bot_exchange.fetch_ohlcv(symbol=pair, timeframe=timeframe, limit=100)
            if not ohlcv: 
                try: log_trade(bot_id, 'DEBUG_ERR', pair, 0, 0, 0, "NO_OHLCV", 0, 0, "Fetch failed")
                except: pass
                return
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])  # type: ignore[arg-type]
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            if strat_type == 'MarketMaker':
                # Pass thread-local exchange to MM logic
                self.process_market_maker(bot_id, name, pair, strategy, df, exchange=bot_exchange)
                return

            trade_data = get_bot_status(bot_id)
            if not trade_data or len(trade_data) < 8: 
                try: log_trade(bot_id, 'DEBUG_ERR', pair, 0, 0, 0, "NO_TRADE_DATA", 0, 0, "Status fetch failed")
                except: pass
                return
            
            # DB indices: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time)
            is_in_trade = trade_data[3] > 0
            current_price = df['close'].iloc[-1]
            
            # Note: Primary state verification happens AFTER open_orders fetch (line ~155)
            
            # --- LOG STATUS ---
            if is_active:
                status_msg = "IN TRADE" if is_in_trade else "Waiting for Signal"
                logger.info(f"Bot {name} ({pair}): {status_msg}")
                
                # Persist status to database for UI display
                try:
                    update_bot_display_status(bot_id, status_msg)
                except Exception:
                    pass  # Non-critical, don't block bot execution


            # --- OPTIMIZATION (v0.7.0): Use Runner Snapshot ---
            # If the runner passed a high-level snapshot, we extract the orders for this pair.
            # Otherwise, we fetch them manually (legacy/standalone compatibility).
            open_orders_snapshot = []
            try:
                if exchange_snapshot and market_type in exchange_snapshot:
                    all_snap_orders = exchange_snapshot[market_type].get('open_orders', [])
                    # Filter for THIS specific pair using normalized comparison
                    open_orders_snapshot = [o for o in all_snap_orders if normalize_symbol(o.get('symbol')) == normalize_symbol(pair)]
                    # logger.debug(f"[{name}] Using snapshotted open orders (Found {len(open_orders_snapshot)})")
                else:
                    open_orders_snapshot = bot_exchange.fetch_open_orders(pair) or []
            except Exception as e:
                logger.warning(f"Could not resolve open orders for {name}: {e}")
                # Don't return, allow logic to proceed (carefully)

            # --- ROBUSTNESS: RECONCILE ORDERS ---
            # Ensure DB state matches Exchange state (Ghost/Stale Order Cleanup)
            try:
                log_trade(bot_id, 'DEBUG_LOG', pair, 0, 0, 0, "CALL_RECONCILE", is_in_trade, 0, "Calling reconcile_orders")
            except: pass
            
            self.reconcile_orders(bot_id, name, pair, is_in_trade, open_orders_snapshot, bot_exchange)
            
            try: log_trade(bot_id, 'DEBUG_LOG', pair, 0, 0, 0, "RECONCILE_DONE", is_in_trade, 0, "Reconcile finished")
            except: pass
            
            # --- SELF-HEALING: Verify State Sync (Fix for Ghost Trades) ---
            if is_in_trade:
                 # Check if this is a "Zombie" state (DB says Trade, Exchange says Empty)
                 # Pass basket_start_time to detect recent entries and avoid false ghost detection
                 basket_start_time = trade_data[8] if len(trade_data) > 8 else 0
                 if not self.verify_state_sync(bot_id, name, pair, bot_exchange, open_orders_snapshot, basket_start_time):
                      is_in_trade = False
                      logger.warning(f"🩹 Bot {name} Auto-Healed: state reset to IDLE.")
                      return # Skip this cycle to allow DB update to propagate
            # -------------------------------------------------------------

            if not is_in_trade:
                # Check for pending entry orders (Non-blocking Chase)
                try:
                    entry_side = 'buy' if direction == 'LONG' else 'sell'
                    pending = [o for o in open_orders_snapshot if isinstance(o, dict) and o.get('side') == entry_side and o.get('type') == 'limit']
                    if pending:
                        self.manage_pending_entry(bot_id, name, pair, direction, pending[0], params, bot_exchange)
                        return 1.0 # High urgency: Pending Entry
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
                    # DEBUG: Log signal values for troubleshooting
                    # (Removed hardcoded debug)
                    if direction == 'LONG' and buy_signal:
                        # First-Claim Policy Disabled for Virtual Positioning
                        # We allow multiple bots to trade the same pair independently
                        if getattr(self.runner, 'trading_enabled', False):
                            self.execute_entry(bot_id, name, pair, 'buy', base_size, exchange=bot_exchange)
                        else:
                            logger.info(f"🛡️ [MONITOR] Bot {name} signal BUY. Entry BLOCKED.")
                    elif direction == 'SHORT' and sell_signal:
                        # First-Claim Policy Disabled
                        if getattr(self.runner, 'trading_enabled', False):
                            self.execute_entry(bot_id, name, pair, 'sell', base_size, exchange=bot_exchange)
                        else:
                            logger.info(f"🛡️ [MONITOR] Bot {name} signal SELL. Entry BLOCKED.")
            else:
                # Check stop-after conditions before managing trade
                from engine.bot_management import check_and_execute_stops
                stop_result = check_and_execute_stops(bot_id, exchange_interface=bot_exchange)
                if stop_result and stop_result.get('action') == 'stop_executed':
                    logger.warning(f"🛑 Stop condition triggered for {name}: {stop_result['reason']}")
                    is_in_trade = False  # Position was closed
                else:
                    strategy.last_market_data = df
                    # DEBUG TRACE
                    try:
                         log_trade(bot_id, 'DEBUG_LOG', pair, current_price, 0, 0, "CALL_MANAGE", trade_data[2], 0, "Calling manage_trade")
                    except: pass
                    
                    mission = manage_trade(bot_id, name, pair, direction, params, trade_data, current_price, strategy, bot_exchange, open_orders=open_orders_snapshot)
                    
                    if mission:
                        try:
                             log_trade(bot_id, 'DEBUG_LOG', pair, 0, 0, 0, "MISSION_RET", trade_data[2], 0, f"Mission: {mission.get('action')}")
                        except: pass
                    
                    if mission and mission.get('action') != 'none':
                        if getattr(self.runner, 'trading_enabled', False):
                            self.execute_mission(mission, exchange=bot_exchange, open_orders_snapshot=open_orders_snapshot)
                        else:
                            logger.info(f"🛡️ [MONITOR] Bot {name} wants to execute mission: {mission.get('action')}. BLOCKED.")

            # --- SMART POLLING (Proximity Awareness) ---
            return self.calculate_polling_interval(bot_id, is_in_trade, current_price, trade_data, params)

        except (ccxt.BadSymbol, ccxt.ExchangeError) as e:
            if "symbol" in str(e).lower() or "market" in str(e).lower():
                logger.error(f"INVALID SYMBOL for bot {name}: {e}. Deactivating.")
                deactivate_bot(bot_id, reason=f"Invalid Symbol: {e}")
            else:
                logger.error(f"Exchange error for bot {name}: {e}")
        except Exception as e:
            import traceback
            logger.error(f"Error processing bot {name}: {e}\n{traceback.format_exc()}")

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
                if getattr(self.runner, 'trading_enabled', False):
                    ex.exchange.cancel_order(order['id'], pair)
                else:
                    logger.info(f"🛡️ [MONITOR] Bot {name} pending entry chase (Cancel {order['id']}) BLOCKED.")
                    return # Stop chase logic here

                
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
                    
                    # Partial Fill Over-Allocation Bug Fix: Calculate remaining USD correctly
                    base_size = float(params.get('base_size', 10.0))
                    avg_price = float(final_order.get('average', 0.0) or current_price)
                    remaining_usd = base_size - (filled * avg_price)
                    
                    if remaining_usd >= 10.0: # Only retry if remaining is above typical MinNotional
                        logger.info(f"Retrying remaining entry amount: ${remaining_usd:.2f}")
                        self.execute_entry(bot_id, name, pair, side, remaining_usd, exchange=ex)
                else:
                    # Retry full amount
                    base_size = float(params.get('base_size', 10.0))
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
            
            # Use passed open_orders or fetch if not provided
            # (In process_bot, we fetch once. In direct calls, we might need to fetch)
            # But process_market_maker signature doesn't accept open_orders yet.
            # Ideally we update signature, but for now let's just fetch if needed.
            open_orders = ex.fetch_open_orders(pair) or []
            current_bids = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'buy']
            current_asks = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'sell']
            
            if not current_bids: 
                if getattr(self.runner, 'trading_enabled', False):
                    self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)
                else:
                    logger.info(f"🛡️ [MONITOR] Bot {name} MM_BID BLOCKED.")
            else:
                best_bid = max(current_bids, key=lambda x: float(x.get('price', 0)))
                bid_price = float(best_bid.get('price', 0)) if best_bid else 0.0
                if bid_price > 0 and abs(bid_price - ideal_bid) / ideal_bid > strategy.reprice_threshold:
                    if getattr(self.runner, 'trading_enabled', False):
                        ex.cancel_all_orders(pair)
                        self.execute_entry(bot_id, name, pair, 'buy', strategy.order_size, price=ideal_bid, params={'postOnly': True}, exchange=ex)
                    else:
                        logger.info(f"🛡️ [MONITOR] Bot {name} MM_BID REPRICE BLOCKED.")

            if not current_asks: 
                if getattr(self.runner, 'trading_enabled', False):
                    self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
                else:
                    logger.info(f"🛡️ [MONITOR] Bot {name} MM_ASK BLOCKED.")
            else:
                best_ask = min(current_asks, key=lambda x: float(x.get('price', 0)))
                ask_price = float(best_ask.get('price', 0)) if best_ask else 0.0
                if ask_price > 0 and abs(ask_price - ideal_ask) / ideal_ask > strategy.reprice_threshold:
                    if getattr(self.runner, 'trading_enabled', False):
                        ex.cancel_all_orders(pair)
                        self.execute_entry(bot_id, name, pair, 'sell', strategy.order_size, price=ideal_ask, params={'postOnly': True}, exchange=ex)
                    else:
                        logger.info(f"🛡️ [MONITOR] Bot {name} MM_ASK REPRICE BLOCKED.")
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

    def _execute_limit_with_chase(self, bot_id, name, pair, side, qty, exchange=None, timeout=None, params={}, initial_price=None, skip_validation=False, client_order_id=None):
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
            ticker = ex.fetch_ticker(pair)
            current_price = 0.0
            if ticker:
                val = ticker.get('bid' if side == 'buy' else 'ask')
                if val is not None: current_price = float(val)
            
            # Fallback to initial_price if fetch failed
            if current_price == 0 and initial_price:
                current_price = initial_price
            
            if current_price == 0: 
                logger.error(f"Price fetch failed for {name} inside chase.")
                return False, 0.0, None
            
            # 2. Validate (unless skip_validation for TP orders)
            if skip_validation:
                # For TP orders closing existing positions, just sanitize precision
                s_amt = float(ex.exchange.amount_to_precision(pair, qty))
                s_price = float(ex.exchange.price_to_precision(pair, current_price))
            else:
                is_valid, s_amt, s_price, err = ex.validate_order(pair, side, qty, current_price)
                if not is_valid:
                    logger.error(f"Validation failed for {name}: {err}")
                    return False, 0.0, None

            # 3. Place Limit Order
            order_params = dict(params) if params else {}
            if skip_validation: order_params['_skip_validation'] = True
            
            # === DETERMINISTIC ID INJECTION ===
            if client_order_id:
                order_params['clientOrderId'] = client_order_id
            # ==================================
            
            order = ex.create_order(pair, 'limit', side, s_amt, s_price, params=order_params, bot_id=bot_id, order_type='LIMIT')
            if not order: 
                logger.error(f"Order creation returned None for {name}.")
                return False, 0.0, None
                
            last_order_id = order.get('id')
            
            # 4. Wait briefly for immediate fill (Chase logic assumes quick fill)
            final_order = ex.wait_for_fill(order, timeout_seconds=5)
            
            if final_order:
                fill_avg = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
                if final_order.get('status') in ['closed', 'filled']:
                    return True, fill_avg, last_order_id
            
            return False, 0.0, last_order_id
            
        except InsufficientFundsError as e:
            logger.error(f"💰 INSUFFICIENT FUNDS for {name}: {e}")
            return False, 0.0, None

        except APIError as e:
            logger.error(f"❌ Exchange API Error for {name}: {e}")
            return False, 0.0, None

        except Exception as e:
            logger.error(f"Limit execution failed for {name}: {e}")
            return False, 0.0, None

    def execute_mission(self, mission, exchange=None, open_orders_snapshot=None):
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
                
                if bot_id in self.runner.strategies:
                    strategy = self.runner.strategies[bot_id]
                    if hasattr(strategy, 'reset_locked_atr'): strategy.reset_locked_atr()
                
                if config.DRY_RUN: reset_bot_after_tp(bot_id, exit_price=exit_price)
                else:
                    side = 'sell' if direction == 'LONG' else 'buy'
                    tp_params = {}
                    
                    # For TP Hit, we don't need strict deterministic ID as it's an exit,
                    # but good practice to tag it if we want detailed history.
                    # Currently using random or auto tag is fine for market/limit close.
                    
                    success, _, _ = self._execute_limit_with_chase(
                        bot_id, bot_name, pair, side, qty, 
                        exchange=ex, initial_price=exit_price,
                        params=tp_params, skip_validation=True
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
                
                open_orders = open_orders_snapshot if open_orders_snapshot is not None else (ex.fetch_open_orders(pair) or [])
                grid_side = 'buy' if direction == 'LONG' else 'sell'
                tp_side = 'sell' if direction == 'LONG' else 'buy'
                
                # Tag construction
                tag_prefix = f"CQB_{bot_id}_"
                
                # --- PHASE 8: DETERMINISTIC ID MATCHING ---
                # We prioritize matching by our Deterministic ID logic
                
                my_grid_orders = []
                my_tp_orders = []
                
                # Generate Expected IDs
                expected_grid_id = self._generate_deterministic_id(bot_id, 'GRID', grid_step) if grid_step else None
                expected_tp_id = self._generate_deterministic_id(bot_id, 'TP', 0) # TP usually single
                
                for o in open_orders:
                    client_oid = o.get('clientOrderId', '')
                    
                    # Match TP
                    if '_TP_' in client_oid and client_oid.startswith(tag_prefix):
                         my_tp_orders.append(o)
                    
                    # Match Grid
                    if '_GRID_' in client_oid and client_oid.startswith(tag_prefix):
                        my_grid_orders.append(o)

                logger.info(f"Maintain {bot_name}: Open={len(open_orders)} | MyGrid={len(my_grid_orders)} MyTP={len(my_tp_orders)}")

                # --- 1. GRID MAINTENANCE ---
                try:
                    grid_needs_replace = False
                    
                    if grid_price and grid_price > 0:
                        if not my_grid_orders:
                            grid_needs_replace = True
                        else:
                            # If existing grid order doesn't match expected ID, replace it (Step changed)
                            o = my_grid_orders[0]
                            if expected_grid_id and o.get('clientOrderId') != expected_grid_id:
                                logger.info(f"Grid Step Change Detected: {o.get('clientOrderId')} -> {expected_grid_id}")
                                grid_needs_replace = True
                            
                            # Price drift check
                            elif abs(float(o.get('price') or 0) - grid_price) / grid_price > 0.001:
                                grid_needs_replace = True
                    
                    if grid_needs_replace:
                        # ABORT ON CANCEL FAILURE LOGIC
                        cancel_success = True
                        if my_grid_orders:
                            logger.info(f"🧹 [Grid] Cancelling {len(my_grid_orders)} stale orders for {bot_name}")
                            for o in my_grid_orders:
                                try: ex.cancel_order(o['id'], pair)
                                except Exception as e:
                                    logger.error(f"❌ Cancel Failed: {e}. ABORTING GRID PLACEMENT.")
                                    cancel_success = False # STOP! Don't place new order if we couldn't clear old one! (Prevent spam)
                        
                        if cancel_success:
                            # Use Deterministic ID
                            det_id = self._gen_id_v2(bot_id, 'GRID', grid_step)

                            # Partial Fill Bug Fix: Check for existing partial fill before placing new order
                            skip_grid_placement = False
                            try:
                                existing_order = ex.fetch_order(det_id, pair)
                                if existing_order:
                                    filled = float(existing_order.get('filled', 0.0))
                                    if filled > 0:
                                        logger.info(f"Found existing partial fill for {det_id}: {filled}")
                                        grid_qty -= filled
                                        if grid_qty <= 0:
                                            logger.info(f"Grid order {det_id} already fully filled. Skipping placement.")
                                            skip_grid_placement = True
                                        else:
                                            logger.info(f"Adjusting grid quantity to {grid_qty:.4f} after partial fill.")
                            except Exception:
                                # Order not found or other fetch error - proceed with full qty
                                pass

                            if not skip_grid_placement:
                                logger.info(f"🆕 [Grid] Placing Grid Step {grid_step}: {grid_qty:.4f} @ {grid_price:.4f}")
                                
                                # Validation
                                is_valid, s_amt, s_price, err = ex.validate_order(pair, grid_side, grid_qty, grid_price)
                                
                                if is_valid:
                                    g_params: Dict[str, Any] = {'postOnly': True}
                                    # Pass ID to create_order (via _execute_limit_with_chase logic, or direct)
                                    # We use direct create here for Grid usually
                                    g_params['clientOrderId'] = det_id
                                    
                                    try:
                                        placed_order = ex.create_order(pair, 'limit', grid_side, s_amt, s_price, params=g_params, bot_id=bot_id, order_type='GRID')
                                        if placed_order:
                                            logger.info(f"✅ Grid Placed: {placed_order.get('id')}")
                                            save_bot_order(bot_id, 'grid', placed_order.get('id'), float(placed_order.get('price')), s_amt, grid_step, client_order_id=det_id)
                                    except APIError as e:
                                        # If Exchange Says "Duplicate Order ID" -> It means we already have it!
                                        if "Duplicate" in str(e) or "Order ID" in str(e):
                                            logger.warning(f"⚠️ Deterministic ID Collision: Order {det_id} already exists on exchange. Assuming success.")
                                            # Ideally fetch it to confirm
                                        else:
                                            logger.error(f"Grid Place Failed: {e}")
                                    except Exception as e:
                                        logger.error(f"Grid Place Failed: {e}")
                                else:
                                    logger.error(f"Grid Invalid: {err}")

                except Exception as e:
                    logger.error(f"Grid Maintenance Failed: {e}")

                # --- 2. TP MAINTENANCE ---
                try:
                    tp_needs_replace = False
                    if tp_price and tp_price > 0:
                        if not my_tp_orders:
                            tp_needs_replace = True
                        else:
                            # Price check
                            o = my_tp_orders[0]
                            if abs(float(o.get('price') or 0) - tp_price) / tp_price > 0.001:
                                tp_needs_replace = True
                    
                    if tp_needs_replace:
                        cancel_success = True
                        if my_tp_orders:
                            logger.info(f"🧹 [TP] Cancelling {len(my_tp_orders)} stale orders for {bot_name}")
                            for o in my_tp_orders:
                                try: ex.cancel_order(o['id'], pair)
                                except Exception as e:
                                    logger.error(f"❌ TP Cancel Failed: {e}. ABORTING TP PLACEMENT.")
                                    cancel_success = False

                        if cancel_success and tp_price > 0:
                            logger.info(f"🆕 [TP] Placing TP: {tp_qty:.4f} @ {tp_price:.4f}")
                            
                            # Deterministic TP ID (Tied to Trade Cycle/Step)
                            # Using 'TP' + current_step ensures only ONE TP per step exists.
                            current_trade_step = trade_data[3] if trade_data else 0 
                            det_tp_id = self._gen_id_v2(bot_id, 'TP', current_trade_step)
                            
                            tp_params = {'clientOrderId': det_tp_id}
                            
                            is_valid, s_amt, s_price, err = ex.validate_order(pair, tp_side, tp_qty, tp_price)
                            if is_valid:
                                try:
                                    new_tp = ex.create_order(pair, 'limit', tp_side, s_amt, s_price, params=tp_params, bot_id=bot_id, order_type='TP')
                                    if new_tp:
                                        nid = new_tp.get('id')
                                        nprice = float(new_tp.get('price') or s_price)
                                        logger.info(f"✅ TP Placed: {nid}")
                                        save_bot_order(bot_id, 'tp', nid, nprice, s_amt, trade_data[2] if trade_data else 0, client_order_id=det_tp_id)
                                        from engine.database import update_trade_tp_price
                                        update_trade_tp_price(bot_id, nprice)
                                except Exception as e:
                                     logger.error(f"TP Place Failed: {e}")
                            else:
                                logger.error(f"TP Invalid: {err}")

                except Exception as e:
                    logger.error(f"TP Maintenance Failed: {e}")

            elif action == 'hedge_open':
                price, qty, amount_usd, step = mission.get('price'), mission.get('qty'), mission.get('amount_usd'), mission.get('step')
                side = 'sell' if direction == 'LONG' else 'buy'
                logger.info(f"[HEDGE] Opening Hedge for {bot_name} at {price}")
                if config.DRY_RUN: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, "DRY_HEDGE", step, 0, "[DRY] Hedge")
                else:
                    hedge_params = {}
                    order = ex.create_order(pair, 'market', side, qty, params=hedge_params, bot_id=bot_id, order_type='HEDGE')
                    if order: log_trade(bot_id, 'HEDGE_OPEN', pair, price, qty, amount_usd, order.get('id'), step, 0, "Hedge Opened")

            elif action == 'reduce_position':
                factor = mission.get('factor', 0.5)
                direction = mission.get('direction')
                current_qty = mission.get('current_qty', 0.0)
                reason = mission.get('reason', 'Drawdown')
                
                reduce_qty = current_qty * factor
                side = 'sell' if direction == 'LONG' else 'buy'
                
                logger.info(f"⚠️ [REDUCE] Reducing {bot_name} by {factor*100}% ({reduce_qty:.4f}) due to {reason}")
                
                if config.DRY_RUN:
                     logger.info(f"[DRY] Would reduce position by {reduce_qty}")
                else:
                     ex = exchange or self.runner.exchange
                     params = {'reduceOnly': True} if ex.market_type == 'future' else {}
                     
                     order = ex.create_order(pair, 'market', side, reduce_qty, params=params, bot_id=bot_id, order_type='REDUCE')
                     if order: 
                         logger.info(f"✅ Reduction Order Placed: {order.get('id')}")

        except Exception as e: logger.error(f"Mission failed for {mission.get('bot_name')}: {e}")

    def _generate_deterministic_id(self, bot_id, type_str, step_index):
        """
        Generates a deterministic Client Order ID (Phase 8 Fix).
        Format: CQB_{bot_id}_{type}_{step}
        Example: CQB_44_GRID_5
        This prevents duplicate orders if the bot restarts or loses DB state.
        The Exchange acts as the deduplication engine.
        """
        return f"CQB_{bot_id}_{type_str.upper()}_{step_index}"

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params=None, exchange=None):
        # === STAGGERED ENTRY DELAY ===
        # Prevents race condition when multiple bots try to enter on same pair simultaneously.
        # Each bot waits a slightly different time based on its ID to spread out order placement.
        stagger_delay = 0.5 + (bot_id % 10) * 0.2  # 0.5s to 2.3s based on bot_id
        logger.debug(f"[{name}] Stagger delay: {stagger_delay:.1f}s before entry attempt")
        time.sleep(stagger_delay)
        
        can_order, reason = self._check_order_limits(bot_id, name)
        if not can_order:
            logger.critical(f"ORDER BLOCKED for {name}: {reason}. PAUSING BOT.")
            from engine.database import deactivate_bot
            deactivate_bot(bot_id, reason=f"Limit Reached: {reason}")
            return
        
        ex = exchange or self.runner.exchange
        logger.info(f"[ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        # === POSITION CONFLICT CHECK (Permanent Fix for One-Way Mode) ===
        # In One-Way mode, if we have an existing position in the OPPOSITE direction,
        # trying to enter will either:
        # 1. Reduce the existing position (if order size <= position size)
        # 2. Flip the position (if order size > position size)
        # But Binance's margin calculation can reject small opposite orders.
        # 
        # Solution: Skip entry if there's an opposite position we didn't create.
        try:
            positions = ex.exchange.fetch_positions([pair])
            for pos in positions:
                contracts = float(pos.get('contracts', 0) or 0)
                if contracts == 0:
                    continue
                    
                pos_side = pos.get('side', '').lower()  # 'long' or 'short'
                
                # Determine if this is a conflict
                # Bot wants to BUY (go LONG) but there's a SHORT position
                # Bot wants to SELL (go SHORT) but there's a LONG position
                is_conflict = (side == 'buy' and pos_side == 'short') or \
                              (side == 'sell' and pos_side == 'long')
                
                if is_conflict:
                    pos_value = contracts * float(pos.get('entryPrice', 0) or 0)
                    logger.warning(
                        f"[POSITION CONFLICT] {name} wants to {side.upper()} but "
                        f"existing {pos_side.upper()} position ({contracts:.4f} contracts, ~${pos_value:.2f}) exists. "
                        f"Skipping entry to avoid margin conflict."
                    )
                    return  # Skip this entry attempt
                    
        except Exception as e:
            logger.warning(f"Could not check position conflict: {e}")
            # Continue with entry attempt if check fails
        
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
        
        if params is None: params = {}
        else: params = params.copy()
        
        # positionSide injection removed to support One-Way Mode and flipping.
        
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
             
             # Deterministic Entry ID (Step 0 + Attempt Index) to allow Chase but prevent ghosts
             # Format: CQB_44_ENTRY_0_0, CQB_44_ENTRY_0_1, etc.
             det_id = self._gen_id_v2(bot_id, 'ENTRY', f"0_{interval_idx}")
             order_params['clientOrderId'] = det_id
             
             try:
                 order = ex.create_order(pair, 'limit', side, qty, target_price, params=order_params, bot_id=bot_id, order_type='ENTRY')
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
                 error_msg = str(e).lower()
                 # CRITICAL: Stop retrying on permanent failures
                 # NOTE: Binance returns 'Margin is insufficient' (-2019), not 'insufficient balance'
                 permanent_failures = [
                     'insufficient balance', 'insufficient funds', 
                     'margin is insufficient', 'insufficient margin'
                 ]
                 if any(pf in error_msg for pf in permanent_failures):
                     logger.error(f"[PERMANENT FAILURE] {name}: {e} - stopping entry attempts")
                     return  # Exit completely, don't retry
                 logger.error(f"Chase attempt failed: {e}")
                 time.sleep(5) # Penalty wait
             
             interval_idx += 1
        
        if success:
            self._finalize_entry(bot_id, name, pair, side, amount, fill_price, order_id, exchange=ex)

    def _finalize_entry(self, bot_id, name, pair, side, amount, fill_price, order_id, exchange=None):
        """
        LAYER 1 FIX: Resilient Entry Finalization with Retry Logic
        
        Previous version caught ALL exceptions and returned early - including transient
        errors like timestamp sync failures (-1021). This caused orders to be placed
        but total_invested to stay at 0.
        
        New version:
        - Retries transient errors (timestamp, network) up to 3 times
        - Only blocks on permanent errors (order not found -2013)
        - Falls back to original fill_price if verification fails
        """
        ex = exchange or self.runner.exchange
        verified_fill_price = None
        verification_succeeded = False
        
        # Retry loop for transient errors
        for attempt in range(3):
            try:
                check = ex.fetch_order(order_id, pair)
                
                if not check:
                    # Order genuinely not found - permanent error
                    logger.critical(f"👻 GHOST ENTRY BLOCKED: Order {order_id} for {name} NOT FOUND on exchange. DB Update Aborted.")
                    return
                
                status = check.get('status')
                if status in ['closed', 'filled']:
                    # Success - extract authoritative fill price
                    verified_fill_price = float(check.get('average', 0) or check.get('price', 0) or fill_price)
                    verification_succeeded = True
                    break
                elif status == 'open':
                    # Order still open - wait and retry
                    logger.info(f"⏳ Order {order_id} still open. Waiting... (attempt {attempt+1}/3)")
                    time.sleep(3)
                    continue
                else:
                    # Cancelled/Expired/Rejected - permanent error
                    logger.critical(f"👻 GHOST ENTRY BLOCKED: Order {order_id} status '{status}'. DB Update Aborted.")
                    return
                    
            except Exception as e:
                error_str = str(e).lower()
                
                # Permanent errors - don't retry
                if 'order does not exist' in error_str or '-2013' in error_str:
                    logger.critical(f"👻 GHOST ENTRY BLOCKED: Order {order_id} does not exist: {e}")
                    return
                
                # Transient errors (timestamp, network) - retry with backoff
                logger.warning(f"⚠️ Finalize verify attempt {attempt+1}/3 failed: {e}. Retrying...")
                time.sleep(1 * (attempt + 1))  # Backoff: 1s, 2s, 3s
        
        # If verification failed but order WAS placed, proceed with original fill_price
        # This is the key fix: we don't abandon the entry just because verification had issues
        if not verification_succeeded:
            logger.warning(f"⚠️ ORDER VERIFICATION FAILED after 3 attempts for {name}. "
                          f"Proceeding with original fill_price ${fill_price:.4f}. "
                          f"Order {order_id} was placed - trade state WILL be updated.")
            verified_fill_price = fill_price
        
        # PROCEED WITH DB UPDATE
        tp_price = verified_fill_price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, verified_fill_price, tp_price)
        self._record_order(bot_id)
        # Save entry order ID for multi-bot tracking
        save_bot_order(bot_id, 'entry', order_id, verified_fill_price, amount/verified_fill_price, step=0, status='filled', client_order_id=f'CQB_{bot_id}_ENTRY_0')
        log_trade(bot_id, 'BUY' if side == 'buy' else 'SELL', pair, verified_fill_price, amount/verified_fill_price, amount, order_id, 0, 0, f"Entry {name}")
        
        # Claim ownership or become passenger based on First-Claim Policy
        success, message = claim_ownership(
            bot_id=bot_id,
            bot_name=name,
            pair=pair,
            entry_order_id=order_id,
            entry_price=verified_fill_price,
            amount_usd=amount,
            tp_price=tp_price
        )
        logger.info(f"🏁 Entry finalized for {name}: {message}")

    def _simulate_dry_run_entry(self, bot_id, name, pair, side, amount, price):
        tp_price = price * (1.015 if side == 'buy' else 0.985)
        update_martingale_step(bot_id, 0, amount, price, tp_price)
        self._record_order(bot_id)
        log_trade(bot_id, 'DRY_BUY' if side == 'buy' else 'DRY_SELL', pair, price, amount/price, amount, "DRY_RUN", 0, 0, f"Dry Entry {name}")

    def _gen_id_v2(self, bot_id, type_str, step_index):
        """
        Generates a deterministic Client Order ID (Phase 8 Fix - V2).
        Format: CQB_{bot_id}_{type}_{step}
        Example: CQB_44_GRID_1
        Forces integer casting to prevent UUID/Hex pollution.
        """
        try:
            # Strip any incidental hex or string trash if present
            s_idx = int(float(str(step_index).split('_')[0])) 
        except:
             # Fallback hash if truly garbage, but keep prefix valid
             s_idx = str(step_index)
             
        return f"CQB_{bot_id}_{type_str.upper()}_{s_idx}"

    def reconcile_orders(self, bot_id, name, pair, is_in_trade, open_orders_snapshot, exchange):
        """
        Robust Reconciliation: Sync DB with Exchange orders.
        1. Clean up STALE orders (Scanning bot shouldn't have Grid/TP).
        2. Detect FILLED/CANCELLED orders that DB thinks are Open.
        3. ADOPT 'Zombie' orders that match our Deterministic ID (The "Real Fix").
        """
        if config.DRY_RUN: return

        try:
            # 1. Fetch DB Orders (What we THINK we have)
            from engine.database import update_order_status, save_bot_order, update_martingale_step
            bot_order_ids = get_bot_order_ids(bot_id)
            track_grid = bot_order_ids.get('grid_orders', [])
            track_tp = bot_order_ids.get('tp_order_id')
            track_entry = bot_order_ids.get('entry_order_id')

            # Build a set of ALL tracked order IDs for this bot
            db_order_map = {} # {id: type}
            if track_tp: db_order_map[track_tp] = 'tp'
            if track_entry: db_order_map[track_entry] = 'entry'
            for g in track_grid: db_order_map[g['order_id']] = 'grid'

            # 2. Check Exchange Orders (What is ACTUALLY there)
            exchange_order_ids = set()
            for o in open_orders_snapshot:
                if 'id' in o: exchange_order_ids.add(o['id'])

            # 3. SYNC: Detect Ghost Orders (In DB, but Missing on Exchange)
            for db_id, o_type in db_order_map.items():
                if db_id not in exchange_order_ids:
                    # Order is MISSING from snapshot. Could be filled, cancelled, OR API Latency.
                    try:
                        # Verify actual status
                        verified_order = exchange.fetch_order(db_id, pair)
                        if verified_order:
                            v_status = verified_order.get('status')
                            if v_status in ['closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected']:
                                logger.debug(f"🧹 Reconcile: Order {db_id} verified {v_status}. Updating DB.")
                                update_order_status(db_id, 'closed') # Update DB to stop tracking
                            else:
                                logger.warning(f"⚠️ Reconcile: Order {db_id} missing from snapshot but verified OPEN on exchange. Latency detected. Keeping in DB.")
                        else:
                            # Order not found even by ID? Then it's truly gone.
                            logger.info(f"🧹 Reconcile: Order {db_id} not found on exchange. Marking closed.")
                            update_order_status(db_id, 'closed')
                            
                    except Exception as e:
                        logger.warning(f"⚠️ Reconcile: Failed to verify status for missing {o_type} {db_id}: {e}. Keeping DB as OPEN for safety.")

            # 4. ENFORCE: Prune "Zombie" Orders & ADOPT Missing Ones
            # Scenario: Bot placed 'CQB_44_GRID_5', crashed, DB lost it over restart.
            # Old Logic: Kill it (Zombie). New Logic: ADOPT IT!
            
            if is_in_trade:
                for o in open_orders_snapshot:
                    oid = o.get('id')
                    client_oid = o.get('clientOrderId', '')
                    
                    # If this order is NOT in our active tracking list
                    if oid not in db_order_map:
                        # Is it ours? (Check Tag)
                        tag_prefix = f"CQB_{bot_id}_"
                        
                        if client_oid.startswith(tag_prefix):
                            # IT IS OURS! But DB doesn't know it. ADOPT IT!
                            logger.info(f"🤝 ADOPTING ORPHAN ORDER: Found {client_oid} ({oid}) on exchange. Restoring to DB.")
                            
                            price = float(o.get('price') or 0.0)
                            amount = float(o.get('amount') or 0.0)
                            
                            # Parse type from tag: CQB_44_GRID_5
                            parts = client_oid.split('_')
                            if len(parts) >= 3:
                                o_type_tag = parts[2] # GRID, TP, ENTRY
                                
                                if o_type_tag == 'TP':
                                    save_bot_order(bot_id, 'tp', oid, price, amount, 0, client_order_id=client_oid)
                                    # Also ensure DB has correct TP price
                                    from engine.database import update_trade_tp_price
                                    update_trade_tp_price(bot_id, price)
                                    logger.info(f"   -> Linked as TP Order. Synced Target TP.")
                                    
                                elif o_type_tag == 'GRID':
                                    step = int(parts[3]) if len(parts) > 3 else 0
                                    save_bot_order(bot_id, 'grid', oid, price, amount, step, client_order_id=client_oid)
                                    logger.info(f"   -> Linked as Grid Order Step {step}.")
                                
                                else:
                                    logger.warning(f"   -> Unknown tag structure {client_oid}, cancelling instead of adopting.")
                                    if getattr(self.runner, 'trading_enabled', False):
                                        try: exchange.exchange.cancel_order(oid, pair)
                                        except: pass
                                    else:
                                        logger.info(f"🛡️ [MONITOR] Bot {name} zombie cleanup (Cancel {oid}) BLOCKED.")

                        
                        else:
                            # Not tagged with deterministic ID. Check legacy ownership or kill.
                            from engine.database import get_order_owner
                            owner_id = get_order_owner(oid)
                            
                            if owner_id == bot_id:
                                # Untracked but owned in DB history? Kill it to be safe (clean slate).
                                logger.warning(f"🧟 ZOMBIE ORDER DETECTED: {name} has untracked order {oid}. Tag: {client_oid}. Pruning.")
                                if getattr(self.runner, 'trading_enabled', False):
                                    try:
                                        exchange.exchange.cancel_order(oid, pair)
                                        update_order_status(oid, 'cancelled')
                                    except Exception as e:
                                        logger.error(f"Failed to prune zombie {oid}: {e}")
                                else:
                                    logger.info(f"🛡️ [MONITOR] Bot {name} zombie pruning (Cancel {oid}) BLOCKED.")


            # ============================================================
            # LAYER 2 FIX: Position-Aware State Recovery (WITH OWNERSHIP CHECK)
            # ============================================================
            # Scenario: Entry order placed successfully, but _finalize_entry failed
            # (timestamp error, crash, etc). Bot has entry_order_id but total_invested=0.
            # Solution: Auto-adopt from exchange position IF no other bot owns it.
            # ============================================================
            if not is_in_trade:
                trade_data = get_bot_status(bot_id)
                if trade_data:
                    entry_oid = bot_order_ids.get('entry_order_id')
                    total_invested = trade_data[3] if len(trade_data) > 3 else 0
                    
                    if entry_oid and total_invested == 0:
                        # Has entry order but no investment - finalization likely failed
                        logger.warning(f"🩹 STATE DESYNC DETECTED: Bot {name} has entry order {entry_oid} but total_invested=0")
                        
                        # CRITICAL: Check if another bot already owns a position on this pair
                        # This prevents multiple bots from adopting the same position!
                        from engine.database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT b.id, b.name 
                            FROM trades t 
                            JOIN bots b ON t.bot_id = b.id 
                            WHERE b.pair = ? AND t.total_invested > 0 AND b.id != ?
                        ''', (pair, bot_id))
                        existing_owner = cursor.fetchone()
                        
                        if existing_owner:
                            logger.info(f"⏭️ Skipping adoption: Bot {existing_owner[1]} (ID {existing_owner[0]}) already owns position on {pair}")
                        else:
                            # Check exchange for position we should own
                            try:
                                positions = exchange.fetch_positions()
                                for pos in positions:
                                    if normalize_symbol(pos.get('symbol')) == normalize_symbol(pair):
                                        size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                                        entry_price = float(pos.get('entryPrice', 0) or pos.get('markPrice', 0) or 0)
                                        
                                        if size > 0 and entry_price > 0:
                                            total = size * entry_price
                                            tp_price = entry_price * 1.015  # Default TP
                                            
                                            logger.info(f"🩹 AUTO-HEALING: Adopting position {size:.6f} @ ${entry_price:.4f} for {name}")
                                            update_martingale_step(bot_id, 0, total, entry_price, tp_price)
                                            log_trade(bot_id, 'STATE_RECOVERY', pair, entry_price, size, total, 
                                                     entry_oid, 0, 0, "Auto-recovered from desync state")
                                            break
                            except Exception as pos_err:
                                logger.error(f"Position recovery failed for {name}: {pos_err}")

        except Exception as e:
            logger.error(f"Reconciliation Error for {name}: {e}")


    def verify_state_sync(self, bot_id, name, pair, exchange, open_orders_snapshot, basket_start_time=0):
        """
        Robustness Check: Detect 'Ghost Trades' where DB says In Trade, but Exchange is Empty.
        
        Args:
            basket_start_time: Timestamp when trade started (used for grace period on new entries)
        
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
                # Standardized Symbol Matching
                if normalize_symbol(p.get('symbol')) == normalize_symbol(pair):
                    size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if size != 0:
                        has_position = True
                        break
            
            # 2. Use Passed Snapshot
            has_orders = len(open_orders_snapshot) > 0
            
            # 3. Decision Logic
            if not has_position and not has_orders:
                # --- ROBUST: Grace Period (Latency Protection) ---
                # API lag can cause a "just filled" order to not yet show in Position.
                # Check BOTH last fill time AND basket start time for maximum robustness.
                should_reset = True
                try:
                    # Check 1: Last filled order grace period
                    from engine.database import get_last_filled_order
                    last_fill = get_last_filled_order(bot_id)
                    if last_fill and 'timestamp' in last_fill:
                        ts_str = str(last_fill['timestamp'])
                        try:
                            filled_time = float(ts_str)
                        except (ValueError, TypeError) as e:
                            logger.debug(f"Failed to parse fill timestamp: {e}")
                            filled_time = pd.to_datetime(ts_str).timestamp()
                            
                        seconds_ago = time.time() - filled_time
                        
                        if seconds_ago < 60: 
                             logger.warning(f"🛡️ Grace Period (Last Fill) Active for {name}: {seconds_ago:.1f}s ago")
                             should_reset = False
                    
                    # Check 2: Basket start time grace period (for new entries)
                    # This handles cases where position isn't immediately visible after entry
                    if should_reset and basket_start_time > 0:
                        seconds_since_start = time.time() - basket_start_time
                        
                        if seconds_since_start < 60:
                            logger.warning(f"🛡️ Grace Period (New Entry) Active for {name}: Started {seconds_since_start:.1f}s ago")
                            should_reset = False
                            
                except Exception as gp_err:
                     logger.warning(f"Grace check error: {gp_err}")
                
                if should_reset:
                    # CRITICAL: DB says Trade, Exchange says NOTHING.
                    logger.critical(f"👻 GHOST TRADE DETECTED for {name} ({pair})! DB In-Trade vs Empty Wallet. Auto-Healing...")
                    
                    # Force Reset DB State
                    # Using 0 as exit price since no real trade exists
                    reset_bot_after_tp(bot_id, exit_price=0, action_label='GHOST_RESET')
                    
                    return False # State was invalid and reset
                else:
                    return True # Grace period saved it
            
            return True # State is valid (or at least has exchange presence)
            
        except Exception as e:
            logger.error(f"State Sync Check Failed for {name}: {e}")
            return True # Fail open (assume valid) to prevent accidental resets during API errors

    def calculate_polling_interval(self, bot_id: int, is_in_trade: bool, current_price: float, trade_data: tuple, params: dict) -> float:
        """
        Determines the optimal polling interval based on market proximity.
        """
        try:
             # Default: Cold
             interval = 15.0
             
             # Extract config params
             trade_data = get_bot_status(bot_id)
             target_tp = trade_data[5] if trade_data else 0
             
             if is_in_trade:
                 # In Trade: Monitor Next Grid & TP
                 # Assume WARM at least
                 interval = 5.0 
                 
                 # If closely approaching TP
                 if target_tp > 0:
                     dist_pct = abs(current_price - target_tp) / current_price
                     if dist_pct < 0.005: return 1.0 # HOT
                     if dist_pct < 0.02: return 5.0 # WARM
             else:
                 # Idle: Monitor Entry
                 interval = 10.0
             
             return interval

        except Exception as e:
             logger.error(f"Polling Calc Error: {e}")
             return 10.0 # Fallback
