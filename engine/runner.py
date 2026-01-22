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
from engine.reconciliation import sync_all_bots
from engine.ownership import (
    init_ownership_tables, OwnershipState, OwnershipEvent,
    claim_ownership, become_passenger, handle_position_closed,
    check_first_claim_policy, reconcile_pair, get_pair_ownership,
    get_ownership_state, update_ownership_state
)
from config.settings import config
from config.constants import (
    MIN_ORDER_USD,
    MAX_ORDERS_PER_CYCLE,
    MAX_ORDERS_PER_BOT_DAILY,
    POLL_INTERVAL_SECONDS,
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
        Synchronizes the state of all active bots with the exchange.
        Uses the new comprehensive reconciliation system.
        """
        logger.info("Starting comprehensive state reconciliation...")
        results = sync_all_bots()
        
        # Log summary
        owner_count = sum(1 for r in results if r.position_owner.value == "owner")
        passenger_count = sum(1 for r in results if r.position_owner.value == "passenger")
        orphan_count = sum(1 for r in results if r.requires_manual_intervention)
        
        logger.info(f"Reconciliation complete: {owner_count} owners, {passenger_count} passengers, {orphan_count} require manual review")
    

    def _reconcile_ownership(self):
        """Reconcile ownership states across all pairs - check for failover and stale records."""
        try:
            # Get all active pairs with ownership
            from engine.ownership import get_all_active_ownerships, reconcile_pair, cleanup_stale_ownerships
            
            active_pairs = get_all_active_ownerships()
            
            for pair_ownership in active_pairs:
                # Reconcile each pair
                result = reconcile_pair(pair_ownership.pair, pair_ownership.exchange_position_exists)
                
                if result["actions_taken"]:
                    for action in result["actions_taken"]:
                        logger.info(f"🔄 Ownership reconciliation: {action}")
                
                if result["new_owner"]:
                    logger.info(f"👑 New owner assigned for {pair_ownership.pair}: Bot {result['new_owner']}")
            
            # Clean up stale ownership records (inactive bots with old positions)
            cleaned = cleanup_stale_ownerships(max_age_seconds=7200)  # 2 hours
            if cleaned > 0:
                logger.info(f"🧹 Cleaned up {cleaned} stale ownership records")
                
        except Exception as e:
            logger.error(f"Ownership reconciliation failed: {e}")



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
            balance_fetch_success = False
            for mt in active_market_types:
                if mt in self.exchanges:
                    try:
                        balance = self.exchanges[mt].fetch_balance()
                        if balance:
                            total_stablecoin += self._calculate_stablecoin_balance(balance)
                            balance_fetch_success = True
                    except Exception: pass
            
            # BUG FIX: If balance fetch failed (auth error), don't trigger circuit breaker
            # Just log and skip this check cycle
            if not balance_fetch_success:
                logger.warning("Circuit breaker check skipped - balance fetch failed (auth/API error)")
                return
                
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
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])  # type: ignore[arg-type]
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
            ex = exchange or self.exchange
            
            trade_data = get_bot_status(bot_id)
            
            if action == 'tp_hit':
                exit_price = mission.get('exit_price')
                qty = mission.get('qty')
                logger.info(f"[TP MISSION] Closing {bot_name} at {exit_price}")
                
                # Reset locked ATR when trade closes
                if bot_id in self.strategies:
                    strategy = self.strategies[bot_id]
                    if hasattr(strategy, 'reset_locked_atr'):
                        strategy.reset_locked_atr()
                
                if config.DRY_RUN: reset_bot_after_tp(bot_id, exit_price=exit_price)
                else:
                    # Use limit order with chase logic for TP - NO market orders unless emergency
                    side = 'sell' if direction == 'LONG' else 'buy'
                    # Use _execute_limit_with_chase (to be defined) instead of market order
                    success, _, _ = self._execute_limit_with_chase(bot_id, bot_name, pair, side, qty, exchange=ex, initial_price=exit_price)
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
                    logger.warning(f"No order IDs tracked for {bot_name}. Using legacy side-based matching.")
                    my_grid_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == grid_side]
                    # For TP orders, be more careful - only match if we own the position
                    # Check if another bot has an entry order on this pair (we're a passenger)
                    can_enter, owner_id, _ = check_first_claim_policy(bot_id, pair)
                    if not can_enter:
                        # We're a passenger - don't manage TP orders, the owner will handle it
                        logger.info(f"👀 {bot_name}: Passenger on {pair}. Skipping TP order management (Owner: Bot {owner_id})")
                        my_tp_orders = []
                    else:
                        # We might be the owner - use side matching as fallback
                        my_tp_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == tp_side]
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
                                    logger.error(f"GRID VALIDATION FAILED for {bot_name}: {err}. Skipping Grid placement.")
                                else:
                                    # Added metadata for better tracking
                                    grid_params = {'postOnly': True, 'clientOrderId': f"grid_{bot_id}_{int(time.time())}"}
                                    try:
                                        grid_order = ex.create_order(pair, 'limit', grid_side, s_amt, s_price, params=grid_params)
                                        if grid_order:
                                            # Save grid order ID for multi-bot tracking
                                            save_bot_order(bot_id, 'grid', grid_order.get('id'), s_price, s_amt, grid_step if grid_step else 0)
                                    except ccxt.InvalidOrder as e:
                                        # Handle Post Only rejection (-5022)
                                        if "-5022" in str(e):
                                            logger.warning(f"Post Only Rejected for {bot_name} at {s_price}. Market moved. Chasing as Maker...")
                                            try:
                                                # Fetch fresh book ticker to get best Maker price
                                                ticker = ex._safe_request('fetch_ticker', symbol=pair)
                                                new_price = 0.0
                                                if ticker:
                                                    if grid_side == 'buy':
                                                        # Join the best bid
                                                        new_price = float(ticker.get('bid') or 0.0)
                                                    else:
                                                        # Join the best ask
                                                        new_price = float(ticker.get('ask') or 0.0)
                                                
                                                if new_price > 0:
                                                    logger.info(f"Retrying Grid at new Maker price: {new_price} (was {s_price})")
                                                    # Retry with postOnly still enabled
                                                    grid_order = ex.create_order(pair, 'limit', grid_side, s_amt, new_price, params=grid_params)
                                                    if grid_order:
                                                        save_bot_order(bot_id, 'grid', grid_order.get('id'), new_price, s_amt, grid_step if grid_step else 0)
                                                    else:
                                                        logger.error(f"Chase Grid order failed (None returned) for {bot_name}")
                                                else:
                                                    logger.error(f"Could not determine new maker price for {bot_name}")
                                            except Exception as retry_e:
                                                logger.error(f"Chase retry failed for {bot_name}: {retry_e}")
                                        else: raise e
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
                                    positions = ex.exchange.fetch_positions([pair])
                                    has_position = any(
                                        float(p.get('size', 0)) != 0 
                                        for p in positions 
                                        if p and p.get('symbol') == pair
                                    )
                                except Exception as pos_err:
                                    logger.warning(f"Could not fetch positions for {bot_name}: {pos_err}")
                                    has_position = True  # Assume position exists if we can't check
                                
                                if not has_position:
                                    logger.warning(f"[TP] Skipping TP order for {bot_name} - no position on exchange (may have already closed)")
                                    return
                                
                                # Use reduceOnly for safety - prevents opening new positions
                                # This was previously disabled due to multi-bot confusion, but 
                                # exchange now rejects TP orders without it when position is at risk
                                tp_params = {
                                    'clientOrderId': f"tp_{bot_id}_{int(time.time())}",
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
        if not hasattr(self, '_last_reset_day') or self._last_reset_day != current_day:
            self.orders_today, self._last_reset_day = {}, current_day
        if self.orders_this_cycle >= MAX_ORDERS_PER_CYCLE: return False, f"Cycle limit ({MAX_ORDERS_PER_CYCLE})"
        bot_count = self.orders_today.get(bot_id, 0)
        if bot_count >= MAX_ORDERS_PER_BOT_DAILY: return False, f"Daily limit ({bot_count})"
        return True, ""

    def _record_order(self, bot_id):
        self.orders_this_cycle += 1
        self.orders_today[bot_id] = self.orders_today.get(bot_id, 0) + 1

    def _execute_limit_with_chase(self, bot_id, name, pair, side, qty, exchange=None, timeout=None, params={}, initial_price=None):
        """
        Executes a Limit Order (Single Shot, Non-Blocking).
        Returns: (success, fill_price, order_id)
        If not successful immediately, order_id is returned but success is False.
        """
        ex = exchange or self.exchange
        
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
            
            # 2. Validate
            is_valid, s_amt, s_price, err = ex.validate_order(pair, side, qty, current_price)
            if not is_valid:
                logger.error(f"Validation failed for {name}: {err}")
                return False, 0.0, None

            # 3. Place Limit Order
            order = ex.create_order(pair, 'limit', side, s_amt, s_price, params=params)
            if not order: 
                logger.error(f"Order creation returned None for {name}. Check Exchange logs.")
                return False, 0.0, None
                
            last_order_id = order.get('id')
            
            # 4. Wait briefly for immediate fill
            filled_fully, final_order = ex.wait_for_fill(last_order_id, pair, timeout_seconds=5)
            
            if final_order:
                fill_avg = float(final_order.get('average', 0.0) or final_order.get('price', 0.0))
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
            logger.critical(f"ORDER BLOCKED for {name}: {reason}")
            return
        
        ex = exchange or self.exchange
        logger.info(f"[ENTRY] Bot: {name} | Side: {side} | Amount: ${amount}")
        
        if price is None: price = ex.get_last_price(pair)
        if price == 0: 
            logger.error(f"Could not get price for {pair}")
            return

        # NEW: Auto-Size to Minimum if configured
        if params.get('use_min_size', False):
            try:
                min_usd = ex.get_min_order_usd(pair, price)
                # Add 5% buffer to be safe against precision rounding
                safe_min = min_usd * 1.05
                # Only override if current amount is LOWER (or just always override? User said "place whatever is minimum")
                # Usually user wants exactly minimum to start small.
                # If they set base_size=10 but min is 100, we override.
                # If they set base_size=500 but min is 100, do we override?
                # User said: "instead of just a adviced minimum order, it also just has an option of minimum starting quantity"
                # "it will just place what every is the minimum"
                # This implies FORCING minimum.
                logger.info(f"Auto-Sizing to Minimum: ${safe_min:.2f} (User Configured)")
                amount = safe_min
            except Exception as e:
                logger.error(f"Failed to auto-size: {e}")

        if config.DRY_RUN:
            self._simulate_dry_run_entry(bot_id, name, pair, side, amount, price)
            return

        # Calculate Qty based on initial price
        qty = amount / price
        
        # Execute using Chase Logic (No Market Fallback)
        # BUG FIX: Use infinite chase (timeout=None) until filled as requested
        success, fill_price, order_id = self._execute_limit_with_chase(bot_id, name, pair, side, qty, exchange=ex, timeout=None, params=params, initial_price=price)
        
        if success:
            self._finalize_entry(bot_id, name, pair, side, amount, fill_price, order_id)
        elif order_id:
            logger.info(f"Entry order {order_id} placed. Monitoring for fill (Non-blocking)...")
        else:
            logger.error(f"Entry failed for {name} after chase attempts. No market fallback allowed.")

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
        
        # Ownership reconciliation: Check for owner failover and stale ownerships
        self._reconcile_ownership()
        
        return True

    def handle_emergency_liquidation(self):
        """
        Emergency liquidation for all active bots.
        BUG FIX: Now properly handles futures positions.
        """
        bots = self.get_active_bots()
        for bot in bots:
            id, name, pair = bot[0], bot[1], bot[2]
            config_json = bot[5]
            config_dict = json.loads(config_json) if config_json else {}
            mt = config_dict.get('market_type', config.MARKET_TYPE)
            ex = self.exchanges.get(mt, self.exchange)
            
            try:
                ex.cancel_all_orders(pair)
                
                if not config.DRY_RUN and mt in ['future', 'swap']:
                    # For futures, fetch positions properly
                    try:
                        positions = ex.exchange.fetch_positions([pair])
                        for pos in positions:
                            if pos and float(pos.get('contracts', 0)) != 0:
                                qty = float(pos['contracts'])
                                side = 'sell' if qty > 0 else 'buy'  # Short if long, Long if short
                                close_qty = abs(qty)
                                logger.warning(f"Emergency Market Close {close_qty} {pair} for {name}")
                                ex.create_order(pair, 'market', side, close_qty)
                    except Exception as pos_err:
                        logger.error(f"Failed to fetch positions for {pair}: {pos_err}")
                        
            except Exception as e: logger.error(f"Cleanup failed for {name}: {e}")

if __name__ == "__main__":
    init_db()
    init_ownership_tables()  # Initialize ownership tracking tables
    logger.info("Bot Service Started.")
    try: runner = BotRunner()
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        sys.exit(1)
    runner.running = True
    PID, STOP, EMERGENCY = config.PATHS["PID_FILE"], config.PATHS["STOP_FILE"], config.PATHS["EMERGENCY_FILE"]
    
    # BUG FIX: Clear emergency file on successful startup (prevents false liquidation on restart)
    if os.path.exists(EMERGENCY):
        os.remove(EMERGENCY)
        logger.info("Cleared stale emergency file")
    
    if os.path.exists(STOP): os.remove(STOP)
    with open(PID, "w") as f: f.write(str(os.getpid()))
    failures = 0
    last_heartbeat = 0
    while runner.running:
        try:
            if not runner.run_cycle(): break
            failures = 0
            
            # Heartbeat every 60s to confirm system is alive
            if time.time() - last_heartbeat > 60:
                logger.info("💓 System Heartbeat - Active")
                last_heartbeat = time.time()
                
        except Exception as e:
            failures += 1
            logger.error(f"Cycle failed ({failures}): {e}")
            if failures >= MAX_CONSECUTIVE_FAILURES: break
        time.sleep(POLL_INTERVAL_SECONDS)
    if os.path.exists(PID): os.remove(PID)
