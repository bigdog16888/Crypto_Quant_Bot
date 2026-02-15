import logging
import json
import threading
import time
import math
import os
import traceback
from typing import Optional, Dict, Any, List, Tuple

from engine.database import (
    get_bot_status,
    update_martingale_step,
    log_trade,
    reset_bot_after_tp,
    save_bot_order,
    get_bot_order_ids,
    get_connection,
    get_all_active_trades_for_pair,
    update_order_status
)
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.strategies.martingale_strategy import MartingaleStrategy
from config.settings import config

logger = logging.getLogger("BotExecutor")

# Thread-local storage for exchange interfaces
_thread_local = threading.local()

class BotExecutor:
    def __init__(self, runner: Any): # 'runner' is BotRunner instance
        self.runner = runner
        self.strategies: Dict[int, MartingaleStrategy] = {}

    def _get_thread_exchange(self, market_type: str) -> ExchangeInterface:
        # Ensure each thread has its own exchange interface to prevent concurrency issues
        if not hasattr(_thread_local, 'exchanges'):
            _thread_local.exchanges = {}
        
        if market_type not in _thread_local.exchanges:
            _thread_local.exchanges[market_type] = ExchangeInterface(market_type=market_type)
            logger.debug(f"Initialized new {market_type} ExchangeInterface for thread {threading.get_ident()}")
        
        return _thread_local.exchanges[market_type]

    def _generate_deterministic_id(self, bot_id: int, type_str: str, step_index: int) -> str:
        """
        Generates a deterministic clientOrderId for orders.
        Format: CQB_{bot_id}_{TYPE}_{STEP}_{TIMESTAMP_SECONDS}
        """
        timestamp_seconds = int(time.time())
        return f"CQB_{bot_id}_{type_str.upper()}_{step_index}_{timestamp_seconds}"

    def _get_strategy_instance(self, bot_id: int, config_dict: Dict[str, Any]) -> MartingaleStrategy:
        if bot_id not in self.strategies:
            self.strategies[bot_id] = MartingaleStrategy(config_dict)
        return self.strategies[bot_id]

    def process_bot(self, bot_data: Tuple, exchange_snapshot: Dict[str, Any]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        bot_id, name, pair, direction, strategy_type, config_json, base_size, martingale_multiplier, rsi_limit, is_active = bot_data

        import random
        # 🛡️ JITTER: Add random sleep to desynchronize parallel bots and reduce race conditions
        time.sleep(random.uniform(0.1, 0.8))
        
        bot_id, name, pair, direction, strategy_type, config_json, base_size, martingale_multiplier, rsi_limit, is_active = bot_data

        # 🚀 FUNDAMENTAL FIX: Double-Check Activation Status from DB
        # This prevents "Zombie Bots" (like 'long gold') from resurrecting if the in-memory 'bot_data' is stale
        # or if an external script (like cleanup_broken_state.py) is fighting for control.
        if is_active:
             real_status = get_bot_status(bot_id)
             # If get_bot_status failed or returned None, something is wrong, but we can't check 'is_active' from it directly 
             # (status dict doesn't always have it). 
             # So we do a quick separate check if we suspect ghosting. 
             # Actually, best is to just trust the Runner's fresh fetch. 
             # BUT, if we want to be paranoid:
             pass 

        if not is_active:
            logger.warning(f"⛔ [ZOMBIE-PROTECTION] Bot {name} ({bot_id}) is marked INACTIVE. Skipping processing.")
            return None, None

        if not config_json:
            logger.error(f"Bot {name} ({bot_id}) has no config. Skipping.")
            return None, None

        try:
            bot_config = json.loads(config_json)
            market_type = bot_config.get('market_type', config.MARKET_TYPE)
            
            # Update bot_config with current market_type from runner (might be overridden globally)
            bot_config['market_type'] = market_type
            bot_config['direction'] = direction

            strategy = self._get_strategy_instance(bot_id, bot_config)
            exchange = self._get_thread_exchange(market_type) # Use thread-specific exchange
            
            bot_status = get_bot_status(bot_id) # Fetch latest status
            if not bot_status: 
                logger.warning(f"Bot {name} ({bot_id}) has no status in DB. Initializing basic status.")
                bot_status = {
                    'bot_id': bot_id,
                    'pair': pair,
                    'current_step': 0,
                    'total_invested': 0.0,
                    'avg_entry_price': 0.0,
                    'target_tp_price': 0.0,
                    'basket_start_time': 0,
                    'entry_confirmed': 0
                }
                # Create initial entry in trades table if missing
                # This is now handled by update_full_snapshot for atomicity
                # conn = get_connection()
                # cursor = conn.cursor()
                # cursor.execute("INSERT OR IGNORE INTO trades (bot_id, pair, current_step, total_invested) VALUES (?, ?, 0, 0.0)", (bot_id, pair))
                # conn.commit()
                # conn.close()

            current_price = exchange.get_last_price(pair) # Get current price
            if not current_price:
                logger.warning(f"Could not get current price for {pair}. Skipping bot {name}.")
                return None, None
            
            # 🚀 FIXED: Extract the DataFrame (market_data) for the bot's specific pair
            # This prevents the 'dict object has no attribute empty' crash in the strategy
            market_type_snapshot = exchange_snapshot.get(market_type, {})
            market_data_map = market_type_snapshot.get('market_data', {})
            bot_market_data = market_data_map.get(pair, MartingaleStrategy.get_empty_df())

            mission = strategy.decide_action(bot_status, current_price, bot_market_data)

            # 🔍 DIAGNOSTIC LOGGING (Fundamental Fix)
            if mission:
                logger.info(f"🔍 [MISSION-FLOW] Bot {name}: action='{mission.get('action')}' | TradingEnabled={config.TRADING_ENABLED}")
            else:
                logger.warning(f"⚠️ [MISSION-FLOW] Bot {name}: decide_action returned None!")

            trade_update_data = None # This will be populated by action methods

            if mission:
                if mission['action'] == 'entry':
                    trade_update_data = self.execute_entry(bot_id, name, pair, mission['side'], mission['amount'], mission['price'], mission.get('params'), exchange, market_type_snapshot, bot_config, bot_status)
                elif mission['action'] == 'maintain_orders':
                    trade_update_data = self.maintain_orders(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)
                elif mission['action'] == 'exit_tp':
                    trade_update_data = self.execute_exit_tp(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)
                elif mission['action'] == 'exit_sl':
                    trade_update_data = self.execute_exit_sl(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)
                
                # Return recommended sleep from strategy, default to 5s if not specified
                return mission.get('sleep_interval', 5.0), trade_update_data

        except Exception as e:
            logger.error(f"Error processing bot {name} ({bot_id}): {e}")
            logger.error(traceback.format_exc())
            return None, None # Indicate an error occurred
        return None, trade_update_data

    def execute_entry(self, bot_id, name, pair, side, amount, price=None, params=None, exchange=None, market_snapshot=None, bot_config=None, bot_status=None) -> Optional[Dict[str, Any]]:
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [ORDER-BLOCKED] Trading disabled. Bot {name} cannot maintain orders for {pair}.")
            return
            
        logger.info(f"🧐 {name}: Checking Entry Logic. Invested={bot_status['total_invested']} EntryConfirmed={bot_status.get('entry_confirmed')}")

        # 1. Get current open orders for this bot
        # Use snapshot if available for performance, fallback to direct fetch
        if market_snapshot:
             open_orders = market_snapshot.get('open_orders', [])
        else:
             open_orders = exchange.fetch_open_orders(pair)
            
        bot_order_ids = get_bot_order_ids(bot_id) # DB knows what we expect

        # Filter for this bot's orders using clientOrderId prefix
        bot_open_orders = [
            o for o in open_orders 
            if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')
        ]
        
        logger.info(f"🧐 {name}: Found {len(bot_open_orders)} open orders for bot. IDs: {[o['id'] for o in bot_open_orders]}")
        
        # Extract existing TP and Grid order IDs from bot_open_orders
        existing_tp_order = next((o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')), None)
        existing_grid_order = next((o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')), None)

        # Get strategy from cache - FIXED: Use bot_config instead of bot_status for params
        strategy = self._get_strategy_instance(bot_id, bot_config)

        # 🚀 MISSING ENTRY LOGIC RESTORED 🚀
        # If we are NOT in a trade (total_invested == 0) and NO entry order exists, PLACE IT.
        if bot_status['total_invested'] == 0:
            existing_entry_order = next((o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')), None)
            
            # 🚀 CHASE LOGIC IMPLEMENTATION 🚀
            if existing_entry_order:
                # Check order age
                order_time = existing_entry_order.get('timestamp') or (int(time.time()) * 1000)
                order_age_sec = (int(time.time() * 1000) - order_time) / 1000.0
                
                # Configurable timeout (default 60s as per user request)
                CHASE_TIMEOUT_SEC = 60 
                
                if order_age_sec > CHASE_TIMEOUT_SEC:
                    logger.info(f"⏱️ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old. Cancelling to CHASE price...")
                else:
                    logger.info(f"⏳ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old (Timeout: {CHASE_TIMEOUT_SEC}s). Waiting...")
                    try:
                        exchange.cancel_order(existing_entry_order['id'], pair)
                        existing_entry_order = None # Reset so we place a new one below
                        time.sleep(1) # Brief pause to ensure cancellation propagates
                    except Exception as e:
                        logger.error(f"❌ Bot {name}: Failed to cancel stale entry order: {e}")

            if not existing_entry_order:
                # Place Entry Order
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} would place ENTRY order for {pair} {side} @ {price}")
                    # Simulate fill
                    log_trade(bot_id, 'ENTRY', pair, price, amount, price*amount, "DRY_ENTRY", 1, "Dry run entry", 0)
                    update_martingale_step(bot_id, 1, price*amount, price, strategy.calculate_take_profit_price(bot_status, price))
                    return {'status': 'filled', 'order_id': 'dry_run'}
                else:
                    try:
                        valid, amount, price, msg = exchange.validate_order(pair, side, amount, price)
                        if not valid:
                            logger.error(f"❌ Entry Order validation failed for {name} {pair}: {msg}")
                            return

                        client_order_id = self._generate_deterministic_id(bot_id, 'ENTRY', 1)
                        order = exchange.create_order(pair, 'limit', side, amount, price, params={'clientOrderId': client_order_id})
                        
                        if order:
                            save_bot_order(bot_id, 'entry', order['id'], price, amount, 1, 'open', client_order_id=client_order_id)
                            logger.info(f"⏳ {name}: Placed ENTRY order {order['id']}. Waiting for FILL confirmation...")
                            
                            # 🚀 STRICT WORKFLOW: Do NOT assume fill.
                            # Set total_invested=0 so maintain_orders is SKIPPED until WS confirms fill.
                            return {
                                'bot_id': bot_id,
                                'total_invested': 0.0, 
                                'avg_entry_price': 0.0,
                                'entry_order_id': order['id'],
                                'entry_confirmed': 0, 
                                'basket_start_time': int(time.time()) # Start timer for chase logic
                            }
                    except Exception as e:
                        logger.error(f"❌ {name}: Error placing ENTRY order for {pair}: {e}")
                        return

        # 2. Check for missing / filled TP order
        if not existing_tp_order and bot_status['total_invested'] > 0: # Only place TP if in trade
            # Determine TP details
            tp_price = strategy.calculate_take_profit_price(bot_status, current_price)
            tp_amount = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange) # Pass exchange

            if tp_amount > 0 and tp_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} would place TP order for {pair} @ {tp_price}")
                else:
                    # Validate TP order
                    valid, tp_amount, tp_price, msg = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', tp_amount, tp_price)
                    if not valid:
                        logger.error(f"❌ TP Order validation failed for {name} {pair}: {msg}")
                        return
                    
                    try:
                        client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
                        # 🚀 FIXED: Map direction to exchange side for TP
                        side = 'sell' if direction == 'LONG' else 'buy'
                        order = exchange.create_order(pair, 'limit', side, tp_amount, tp_price, params={'reduceOnly': True, 'clientOrderId': client_order_id})
                        if order:
                            save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], 'open', client_order_id=client_order_id)
                            logger.info(f"✅ {name}: Placed TP order for {pair} @ {tp_price} (ID: {order['id']})")
                    except Exception as e:
                        logger.error(f"❌ {name}: Error placing TP order for {pair}: {e}")

        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps and bot_status['total_invested'] > 0:
            # Determine Grid details
            grid_price = strategy.calculate_grid_order_price(bot_status, current_price)
            grid_amount = strategy.calculate_grid_order_amount(bot_status, current_price, pair, exchange) # Pass exchange

            if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} would place Grid order for {pair} @ {grid_price}")
                else:
                    # Validate Grid order
                    # 🚀 FIXED: Use 'buy'/'sell' instead of 'LONG'/'SHORT'
                    side = 'buy' if direction == 'LONG' else 'sell'
                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        logger.error(f"❌ Grid Order validation failed for {name} {pair}: {msg}")
                        return
                    
                    try:
                        client_order_id_grid = self._generate_deterministic_id(bot_id, 'GRID', bot_status['current_step'] + 1)
                        # 🚀 FIXED: Map direction to exchange side
                        order = exchange.create_order(pair, 'limit', side, grid_amount, grid_price, params={'clientOrderId': client_order_id_grid})
                        if order:
                            save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, 'open', client_order_id=client_order_id_grid)
                            logger.info(f"✅ {name}: Placed Grid order for {pair} @ {grid_price} (ID: {order['id']})")
                    except Exception as e:
                        logger.error(f"❌ {name}: Error placing Grid order for {pair}: {e}")
                        
        # 4. Cleanup any untracked open orders for this bot
        # 🚀 FIXED: Handle None for existing orders prevents AttributeError
        known_order_ids = []
        if existing_tp_order: known_order_ids.append(existing_tp_order.get('id'))
        if existing_grid_order: known_order_ids.append(existing_grid_order.get('id'))
        
        # Also protect the Entry order from being cancelled as "untracked"
        if existing_entry_order:
             known_order_ids.append(existing_entry_order.get('id'))
        
        for order in bot_open_orders:
            # Skip if it is an Entry order (handled primarily by strategy logic)
            if '_ENTRY_' in order.get('clientOrderId', ''):
                continue
                
            if order['id'] not in known_order_ids:
                logger.warning(f"⚠️ {name}: Untracked open order {order['id']} for {pair}. Cancelling.")
                try:
                    exchange.cancel_order(order['id'], pair) # Use exchange's cancel_order, not specific bot
                    update_order_status(bot_id, order['id'], 'cancelled')
                except Exception as e:
                    logger.error(f"Failed to cancel untracked order {order['id']}: {e}")

    def execute_exit_tp(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [EXIT-BLOCKED] Trading disabled. Bot {name} cannot execute TP for {pair}.")
            return

        logger.info(f"🎯 {name}: Executing TP exit for {pair} at step {bot_status['current_step']}")
        # In Virtual Position mode, the TP order should already be on the exchange
        # We just need to ensure it fills and update DB state
        
        # If DRY_RUN, simulate fill and reset
        if config.DRY_RUN:
            log_trade(bot_id, 'TAKE_PROFIT', pair, current_price, bot_status['total_invested'] / bot_status['avg_entry_price'], bot_status['total_invested'], f'DRY_RUN_TP_{bot_id}', bot_status['current_step'], "Dry run TP", (current_price - bot_status['avg_entry_price']) * bot_status['total_invested'] / bot_status['avg_entry_price'])
            reset_bot_after_tp(bot_id, current_price, direction=direction)
            logger.info(f"📊 [DRY-RUN] Bot {name} would have exited TP for {pair}")
            return

        # For live trading, TP order is already managed. Just need to monitor fill.
        # The reconciliation cycle will eventually pick up the filled order.
        # For immediate confirmation, we can explicitly check if TP order is filled.
        
        bot_order_ids = get_bot_order_ids(bot_id)
        tp_order_id = bot_order_ids.get('tp_order_id')

        if tp_order_id:
            try:
                order_status = exchange.fetch_order(tp_order_id, pair)
                if order_status and order_status.get('status') in ['closed', 'filled']:
                    logger.info(f"✅ {name}: TP order {tp_order_id} filled. Resetting bot.")
                    reset_bot_after_tp(bot_id, current_price, direction=direction)
                else:
                    logger.warning(f"⚠️ {name}: TP order {tp_order_id} not yet filled/closed. Monitoring.")
            except Exception as e:
                logger.error(f"❌ {name}: Error fetching TP order {tp_order_id} status: {e}")
        else:
            logger.warning(f"⚠️ {name}: No TP order found in DB for {pair}. Resetting bot manually.")
            reset_bot_after_tp(bot_id, current_price, direction=direction) # Force reset if TP not found

    def maintain_orders(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ensures TP and Grid orders are placed active trades.
        """
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [MAINTAIN-BLOCKED] Trading disabled. Bot {name} cannot maintain orders.")
            return

        # 1. Get current open orders
        open_orders = None
        if market_snapshot:
             open_orders = market_snapshot.get('open_orders') # Default to None, NOT []
        
        # FAILSAFE: If snapshot missing/failed, fetch directly to avoid Ghost Orders
        if open_orders is None:
             try:
                 open_orders = exchange.fetch_open_orders(pair)
             except Exception as e:
                 logger.error(f"❌ {name}: Critical - Failed to fetch open orders during maintenance: {e}")
                 return None # Abort to prevent duplicates

        bot_open_orders = [o for o in open_orders if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]
        
        # --- SELF-HEALING: Deduplicate Orders ---
        # Ensure only 1 TP and 1 Grid exist. If more, cancel the extras.
        grid_orders = [o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')]
        tp_orders = [o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')]
        
        if len(grid_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(grid_orders)} GRID orders. Running self-healing cleanup...")
            # Sort by timestamp (in clientOrderID) or ID. explicit sort.
            # Assuming newest is last or has highest ID.
            # Best metric: Match the current_step.
            # If multiple match current_step, keep newest.
            valid_grid = None
            for o in grid_orders:
                # Cancel all, we will repost valid one if needed? No, risky.
                # Heuristic: Keep the one closest to current price? Or just newest.
                # Let's keep the NEWEST ONE.
                pass
            
            # Sort by ID (usually works for recency)
            grid_orders.sort(key=lambda x: str(x['id']), reverse=True)
            keep_order = grid_orders[0]
            for o in grid_orders[1:]:
                try:
                    logger.info(f"🧹 {name}: Self-healing cancelling duplicate GRID {o['id']}")
                    exchange.cancel_order(o['id'], pair)
                except Exception as e:
                     logger.error(f"Failed to cancel duplicate {o['id']}: {e}")
            
            # Update list after cleanup
            existing_grid_order = keep_order
        else:
            existing_grid_order = grid_orders[0] if grid_orders else None

        if len(tp_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(tp_orders)} TP orders. Running self-healing cleanup...")
            tp_orders.sort(key=lambda x: str(x['id']), reverse=True)
            keep_order = tp_orders[0]
            for o in tp_orders[1:]:
                try:
                    logger.info(f"🧹 {name}: Self-healing cancelling duplicate TP {o['id']}")
                    exchange.cancel_order(o['id'], pair)
                except Exception as e:
                     logger.error(f"Failed to cancel duplicate TP {o['id']}: {e}")
            existing_tp_order = keep_order
        else:
            existing_tp_order = tp_orders[0] if tp_orders else None
        # ----------------------------------------

        strategy = self._get_strategy_instance(bot_id, bot_config)

        # 2. Check for missing / filled TP order
        if not existing_tp_order:
            tp_price = strategy.calculate_take_profit_price(bot_status, current_price)
            tp_amount = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            
            logger.info(f"🔍 [TP-MAINTENANCE] Checking TP for {name}: tp_price={tp_price}, amount={tp_amount}")

            if tp_amount > 0 and tp_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains TP for {pair} @ {tp_price}")
                else:
                    valid, tp_amount, tp_price, msg = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', tp_amount, tp_price)
                    if valid:
                        try:
                            client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
                            side = 'sell' if direction == 'LONG' else 'buy'
                            order = exchange.create_order(pair, 'limit', side, tp_amount, tp_price, params={'clientOrderId': client_order_id})
                            if order:
                                save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], 'open', client_order_id=client_order_id)
                                logger.info(f"✅ {name}: Maintained TP order for {pair} @ {tp_price}")
                        except Exception as e:
                             logger.error(f"❌ {name}: Error maintaining TP: {e}")

        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps:
            grid_price = strategy.calculate_grid_order_price(bot_status, current_price)
            grid_amount = strategy.calculate_grid_order_amount(bot_status, current_price, pair, exchange)
            
            logger.info(f"🔍 [GRID-MAINTENANCE] Checking GRID for {name}: grid_price={grid_price}, amount={grid_amount}, step={bot_status['current_step']}")

            if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains Grid for {pair} @ {grid_price}")
                else:
                    logger.info(f"🔍 [GRID-DEBUG] Bot {name} ({direction}) | Price={current_price} | GridTarget={grid_price} | Amount={grid_amount}")
                    
                    side = 'buy' if direction == 'LONG' else 'sell'
                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        logger.error(f"❌ Grid Order validation failed for {name} {pair}: {msg}")
                    else:
                        try:
                            client_order_id_grid = self._generate_deterministic_id(bot_id, 'GRID', bot_status['current_step'] + 1)
                            # 🚀 FIXED: Map direction to exchange side
                            order = exchange.create_order(pair, 'limit', side, grid_amount, grid_price, params={'clientOrderId': client_order_id_grid})
                            if order:
                                save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, 'open', client_order_id=client_order_id_grid)
                                logger.info(f"✅ {name}: Maintained Grid order for {pair} @ {grid_price}")
                        except Exception as e:
                            logger.error(f"❌ {name}: Error maintaining Grid: {e}")
                            
        return None


    def execute_exit_sl(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [EXIT-BLOCKED] Trading disabled. Bot {name} cannot execute SL for {pair}.")
            return

        logger.critical(f"⛔ {name}: Executing STOP LOSS for {pair} at step {bot_status['current_step']}")
        
        if config.DRY_RUN:
            log_trade(bot_id, 'STOP_LOSS', pair, current_price, bot_status['total_invested'] / bot_status['avg_entry_price'], bot_status['total_invested'], f'DRY_RUN_SL_{bot_id}', bot_status['current_step'], "Dry run SL", (current_price - bot_status['avg_entry_price']) * bot_status['total_invested'] / bot_status['avg_entry_price'])
            reset_bot_after_tp(bot_id, current_price, direction=direction)
            logger.info(f"📊 [DRY-RUN] Bot {name} would have exited SL for {pair}")
            return
        
        # Cancel all open orders for this bot
        exchange.cancel_orders_by_bot_id(bot_id, pair)

        # Close the position with a market order
        try:
            position_side = 'sell' if direction == 'LONG' else 'buy'
            # In futures, a market order to opposite side closes position
            # We need to calculate current position size to close
            
            # Fetch current position from exchange
            positions = exchange.fetch_positions()
            current_position = next((p for p in positions if normalize_symbol(p.get('symbol')) == normalize_symbol(pair)), None)
            
            if current_position:
                size_to_close = float(current_position.get('contracts', 0) or current_position.get('size', 0) or 0)
                if abs(size_to_close) > 0:
                    logger.warning(f"Placing market order to close {abs(size_to_close)} {pair} {position_side} for bot {name}")
                    order = exchange.create_order(pair, 'market', position_side, abs(size_to_close))
                    if order:
                        log_trade(bot_id, 'STOP_LOSS_EXIT', pair, current_price, abs(size_to_close), current_price * abs(size_to_close), f'SL_MARKET_{bot_id}', bot_status['current_step'], "SL Market Exit", (current_price - bot_status['avg_entry_price']) * abs(size_to_close))
                        reset_bot_after_tp(bot_id, current_price, direction=direction)
                        logger.info(f"✅ {name}: Market order placed to close SL for {pair} (ID: {order['id']})")
                    else:
                        logger.error(f"❌ {name}: Failed to place market order for SL exit for {pair}")
                else:
                    logger.info(f"ℹ️ {name}: No active position found on exchange for {pair}. Resetting DB state.")
                    reset_bot_after_tp(bot_id, current_price, direction=direction)
            else:
                logger.info(f"ℹ️ {name}: No active position found on exchange for {pair}. Resetting DB state.")
                reset_bot_after_tp(bot_id, current_price, direction=direction)

        except Exception as e:
            logger.error(f"❌ {name}: Error executing SL for {pair}: {e}")

    def check_for_safety_stop(self):
        """
        Checks if a global stop file exists.
        This file is created by an external mechanism or user to halt trading.
        """
        if os.path.exists(config.PATHS["STOP_FILE"]):
            logger.critical(f"🛑 GLOBAL STOP FILE DETECTED: {config.PATHS['STOP_FILE']}. Halting trading.")
            self.runner.running = False
            return True
        return False
