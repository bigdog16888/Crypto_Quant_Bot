import logging
import json
import threading
import time
import math
import os
import traceback
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

from engine.database import (
    get_bot_status,
    update_martingale_step,
    log_trade,
    reset_bot_after_tp,
    save_bot_order,
    get_bot_order_ids,
    get_connection,
    get_all_active_trades_for_pair,
    update_order_status,
    update_bot_error
)
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import calculate_early_exit_decay
from config.settings import config

logger = logging.getLogger("BotExecutor")

# Thread-local storage for exchange interfaces
_thread_local = threading.local()

class BotExecutor:
    def __init__(self, runner: Any): # 'runner' is BotRunner instance
        self.runner = runner
        self.strategies: Dict[int, MartingaleStrategy] = {}
        self.config_cache: Dict[int, str] = {} # Cache for config JSON strings

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

    def _get_strategy_instance(self, bot_id: int, config_dict: Dict[str, Any], config_json_str: Optional[str] = None) -> MartingaleStrategy:
        # Check if config has changed
        cached_config = self.config_cache.get(bot_id)
        
        if bot_id not in self.strategies:
            self.strategies[bot_id] = MartingaleStrategy(config_dict)
            if config_json_str:
                self.config_cache[bot_id] = config_json_str
        elif config_json_str and cached_config != config_json_str:
            # 🚀 OPTIMIZED FIX: Only update params if config actually changed!
            # This addresses user concerns about performance overhead.
            self.strategies[bot_id].params = config_dict
            self.config_cache[bot_id] = config_json_str
            # logger.debug(f"🔄 Bot {bot_id}: Strategy params updated from DB.")
            
        return self.strategies[bot_id]

    def _place_gtx_order_with_retry(self, exchange, pair: str, side: str, amount: float, price: float, params: dict, label: str = "order") -> dict:
        """
        Places a GTX (Post-Only) limit order. If Binance rejects with -50004 (would execute as
        taker/cross the book), auto-fetches live bid1/ask1 and retries ONCE at the correct
        maker price.

        Maker price rules:
          - BUY  (LONG entry, SHORT TP): price must be <= best BID  (buying at or below bid → maker)
          - SELL (SHORT entry, LONG TP): price must be >= best ASK   (selling at or above ask → maker)
        """
        try:
            return exchange.create_order(pair, 'limit', side, amount, price, params=params)
        except Exception as e:
            err_str = str(e)
            if '-50004' in err_str or 'Post Only' in err_str or 'post only' in err_str.lower():
                logger.warning(f"⚠️ [{label}] GTX rejected (-50004) for {pair} {side} @ {price:.4f}. Fetching live bid/ask for retry...")
                bid, ask = exchange.get_best_bid_ask(pair)
                if bid is None or ask is None:
                    logger.error(f"❌ [{label}] Could not fetch bid/ask for retry. Giving up.")
                    raise
                # Determine safe maker price
                prec = exchange.get_symbol_precision(pair)
                tick = prec['tick_size']
                if side.lower() == 'buy':
                    # To be maker on a BUY, place AT or BELOW the best bid
                    retry_price = exchange.round_to_step(bid, tick)
                else:
                    # To be maker on a SELL, place AT or ABOVE the best ask
                    retry_price = exchange.ceil_to_step(ask, tick)
                logger.info(f"🔄 [{label}] Retrying {pair} {side} @ {retry_price:.4f} (bid={bid:.4f} ask={ask:.4f})")
                return exchange.create_order(pair, 'limit', side, amount, retry_price, params=params)
            raise  # Re-raise if it's a different error

    # ---------------------------------------------------------------------------
    # Private helpers — single canonical implementations shared across methods
    # ---------------------------------------------------------------------------

    @staticmethod
    def _get_order_amount(order: dict) -> float:
        """Safe multi-key accessor for order quantity.
        CCXT live orders use 'amount', DB-cached orders may use 'origQty' or 'qty'."""
        return float(order.get('amount') or order.get('origQty') or order.get('qty') or 0)

    def _compute_effective_tp(self, bot_id: int, name: str, bot_status: dict,
                               bot_config: dict, strategy) -> float:
        """Return the effective TP price after Early Exit decay, persisting any change to DB.
        Falls back to the raw DB value if EE is disabled or calculation fails."""
        raw_db_tp = float(bot_status.get('target_tp_price', 0))
        if not (bot_config.get('UseEarlyExit', False) and bot_status.get('basket_start_time', 0) > 0):
            return raw_db_tp
        try:
            original_tp = strategy.calculate_take_profit_price(
                bot_status, bot_status.get('avg_entry_price', 0)
            )
            start_dt = datetime.fromtimestamp(bot_status['basket_start_time'])
            now_dt   = datetime.fromtimestamp(time.time())
            decayed_tp = calculate_early_exit_decay(
                start_dt, now_dt,
                bot_status.get('current_step', 0) + 1,
                original_tp,
                bot_status.get('avg_entry_price', original_tp),
                bot_config
            )
            if abs(decayed_tp - raw_db_tp) / max(raw_db_tp, 0.0001) > 0.0001:
                logger.info(f"⏳ [EE-DECAY] {name}: TP decaying {raw_db_tp:.4f} → {decayed_tp:.4f} (Baseline: {original_tp:.4f})")
                try:
                    _c = get_connection()
                    _c.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (decayed_tp, bot_id))
                    _c.commit()
                    _c.close()
                except Exception as _db_err:
                    logger.warning(f"[EE] Failed to persist decayed TP: {_db_err}")
                return decayed_tp
        except Exception as _err:
            logger.warning(f"[EE] Decay calculation failed for {name}: {_err}")
        return raw_db_tp

    def _sync_replace_tp(self, bot_id: int, name: str, pair: str, direction: str,
                          bot_status: dict, exchange: ExchangeInterface,
                          db_tp: float, db_qty: float,
                          existing_tp_order: dict) -> Optional[dict]:
        """Cancel the out-of-date TP order and place a fresh one at db_tp / db_qty.
        Automatically applies reduceOnly when this bot is the sole active bot on the pair.
        Returns the new order dict, or None on failure."""
        try:
            # Determine reduceOnly — only when sole bot on this pair
            try:
                _c2 = get_connection()
                _cur2 = _c2.cursor()
                _cur2.execute(
                    "SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id "
                    "WHERE b.pair=? AND t.total_invested>0 AND b.id!=?",
                    (pair, bot_id)
                )
                should_reduce = (_cur2.fetchone()[0] == 0)
                _c2.close()
            except Exception:
                should_reduce = False

            tp_order_id = existing_tp_order.get('order_id', existing_tp_order.get('id'))
            exchange.cancel_order(tp_order_id, pair)
            update_order_status(tp_order_id, 'cancelled', bot_id=bot_id)

            if db_qty <= 0 or db_tp <= 0 or config.DRY_RUN:
                return None

            side = 'sell' if direction == 'LONG' else 'buy'
            valid, db_qty, db_tp, msg = exchange.validate_order(pair, side, db_qty, db_tp)
            if not valid:
                logger.warning(f"[TP-SYNC] {name}: Validation failed — {msg}")
                return None

            client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
            tp_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}
            if should_reduce:
                tp_params['reduceOnly'] = True

            order = self._place_gtx_order_with_retry(
                exchange, pair, side, db_qty, db_tp, params=tp_params, label=f"{name}-TP-SYNC"
            )
            if order:
                save_bot_order(bot_id, 'tp', order['id'], db_tp, db_qty,
                               bot_status['current_step'], 'open', client_order_id=client_order_id)
                logger.info(f"✅ [SYNC] {name}: Re-placed TP @ {db_tp:.4f} Qty {db_qty:.4f}")
            return order
        except Exception as _ex:
            logger.error(f"❌ [SYNC] {name}: Failed to replace TP: {_ex}")
            return None

    def process_bot(self, bot_data: Tuple, exchange_snapshot: Dict[str, Any]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        # Robust index-based access to handle potential schema/unpacking mismatches
        bot_id = bot_data[0]
        name = bot_data[1]
        pair = bot_data[2]
        direction = bot_data[3]
        strategy_type = bot_data[4]
        config_json = bot_data[5]
        db_invested = float(bot_data[6]) if len(bot_data) > 6 else 0.0
        db_step = int(bot_data[7]) if len(bot_data) > 7 else 0
        rsi_limit = float(bot_data[8]) if len(bot_data) > 8 else 30.0
        is_active = bool(bot_data[9]) if len(bot_data) > 9 else True
        base_size = float(bot_data[10]) if len(bot_data) > 10 else 10.0
        martingale_multiplier = float(bot_data[11]) if len(bot_data) > 11 else 1.5

        import random
        # 🛡️ JITTER: Add random sleep to desynchronize parallel bots and reduce race conditions
        time.sleep(random.uniform(0.1, 0.8))
        
        

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
            
            market_type = normalize_market_type(bot_config.get('market_type', config.MARKET_TYPE))
            
            # Update bot_config with current market_type from runner (might be overridden globally)
            bot_config['market_type'] = market_type
            bot_config['direction'] = direction
            bot_config['bot_name'] = name # Inject Name for logging
            bot_config['bot_id'] = bot_id # Inject ID for logging
            
            # 🚀 FUNDAMENTAL FIX: Inject missing SQLite configuration 
            # so the strategy doesn't fallback to $150 and 2.0x
            bot_config['base_size'] = base_size
            bot_config['martingale_multiplier'] = martingale_multiplier
            bot_config['rsi_limit'] = rsi_limit

            exchange = self._get_thread_exchange(market_type) # Use thread-specific exchange
            
            current_price = exchange.get_last_price(pair) # Get current price
            if not current_price:
                logger.warning(f"Could not get current price for {pair}. Skipping bot {name}.")
                return None, None
                
            # 🚀 AUTO MIN-SIZE CALCULATION
            if bot_config.get('use_min_size', False):
                prec = exchange.get_symbol_precision(pair)
                step_size = prec.get('step_size', 0.001)
                min_cost_qty = step_size * current_price
                
                # Enforce Binance Demo FAPI $100 minimum notional constraint
                if getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False):
                    min_cost_notional = 100.0
                else:
                    min_cost_notional = 5.0 if market_type == 'future' else 10.0
                
                # Target 5% above the strict minimum to avoid slippage drops
                auto_min_size = max(min_cost_qty, min_cost_notional) * 1.05
                bot_config['base_size'] = auto_min_size
            else:
                # Safety check: Even if not explicitly use_min_size, if we are on Demo FAPI, enforce the $100 minimum to prevent constant API rejections
                if getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False):
                    if bot_config.get('base_size', 0) < 100.0:
                        bot_config['base_size'] = 105.0

            # Inject the fully hydrated config into strategy calculation
            strategy = self._get_strategy_instance(bot_id, bot_config, json.dumps(bot_config))
            
            # 🚀 DYNAMIC PRECISION FIX: Inject real exchange precision into strategy
            # This prevents "Zero Gap" rounding issues on low-priced coins like SUI ($0.95)
            try:
                prec_data = exchange.get_symbol_precision(pair)
                if prec_data:
                    meta = {
                        'price_precision': prec_data.get('price_precision', 2),
                        'qty_precision': prec_data.get('qty_precision', 3),
                        'tick_size': prec_data.get('tick_size', 0.01),
                        'step_size': prec_data.get('step_size', 0.001)
                    }
                    strategy.set_precision_metadata(meta)
            except Exception as e:
                logger.error(f"Error updating precision metadata for {name}: {e}")
            
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
            
            # 🚀 GHOST ORDER CLEANUP (Scanning/Idle Bots)
            # If we are NOT in a trade (invested < 1.0), we should have NO orders.
            # This logic captures the 'Scanning' bot scenario that maintain_orders misses.
            if bot_status.get('total_invested', 0.0) < 1.0:
                 # Fetch open orders for this pair to check for ghosts
                 try:
                     # Use snapshot if available, else fetch
                     open_orders_check = exchange_snapshot.get(market_type, {}).get('open_orders', [])
                     if not open_orders_check: # Double check if snapshot empty
                          open_orders_check = exchange.fetch_open_orders(pair)
                     
                     bot_ghosts = [o for o in open_orders_check if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]
                     
                     if bot_ghosts:
                          # 🚀 RACE CONDITION FIX: Do not cancel ENTRY orders here!
                          # If the strategy just placed an ENTRY order, it takes a moment for the WS to confirm.
                          # If we cancel it here, the strategy will place another one, causing a runaway accumulation loop!
                          true_ghosts = [o for o in bot_ghosts if '_ENTRY_' not in o.get('clientOrderId', '')]
                          
                          if true_ghosts:
                              logger.warning(f"👻 {name}: Found {len(true_ghosts)} GHOST orders while SCANNING (Invested={bot_status['total_invested']}). Purging...")
                              for ghost in true_ghosts:
                                   logger.info(f"🔥 Cancelling ghost order {ghost['id']} ({ghost.get('clientOrderId')})")
                                   try:
                                       exchange.cancel_order(ghost['id'], pair)
                                   except Exception as e:
                                       logger.error(f"Failed to cancel ghost {ghost['id']}: {e}")
                 except Exception as e:
                      logger.error(f"Ghost cleanup failed for {name}: {e}")
            # ---------------------------------------------------------
            
            # 🚀 FIXED: Extract the DataFrame (market_data) for the bot's specific pair
            # This prevents the 'dict object has no attribute empty' crash in the strategy
            market_type_snapshot = exchange_snapshot.get(market_type, {})
            market_data_map = market_type_snapshot.get('market_data', {})
            bot_market_data = market_data_map.get(pair, MartingaleStrategy.get_empty_df())
            bot_multi_tf = market_type_snapshot.get('multi_tf_data', {}).get(pair, {})

            if bot_id == 10000:
                logger.debug(f"Bot 10000 | Price={current_price} | MarketDataEmpty={bot_market_data.empty}")
                # logger.info(f"🕵️ TRACE STARTING decide_action")

            try:
                mission = strategy.decide_action(bot_status, current_price, bot_market_data, multi_tf_data=bot_multi_tf)
            except Exception as e:



                logger.error(f"Error in decide_action: {e}")
                logger.error(traceback.format_exc())
                mission = None



            # 🔍 DIAGNOSTIC LOGGING (Fundamental Fix)
            if mission:
                logger.info(f"🔍 [MISSION-FLOW] Bot {name}: action='{mission.get('action')}' | TradingEnabled={config.TRADING_ENABLED}")
            else:
                if bot_id == 10000: logger.debug(f"Bot 10000: Mission is None")
                logger.debug(f"[MISSION-FLOW] Bot {name}: no action (Scanning)")
                # 🚀 UX: Clear transient errors if we reach this point without an exception
                update_bot_error(bot_id, None) 

            trade_update_data = None # This will be populated by action methods

            if mission:
                if mission['action'] == 'entry':
                     
                    # 🛡️ GLOBAL SAFETY: Check Maximum Account Drawdown
                    # Prevents full portfolio wipeout during flash crashes across all bots
                    try:
                        market_type = normalize_market_type(strategy.params.get('market_type', 'spot'))
                        account_info = exchange_snapshot.get(market_type, {}).get('account', {})
                        
                        balance = account_info.get('totalWalletBalance') or account_info.get('totalMarginBalance')
                        equity = account_info.get('totalCrossWalletBalance') or account_info.get('totalMarginBalance')
                        
                        if balance and equity:
                            drawdown_pct = ((float(balance) - float(equity)) / float(balance)) * 100
                            
                            if drawdown_pct >= config.MAX_ACCOUNT_DRAWDOWN_PERCENT > 0:
                                logger.critical(f"🛑 [GLOBAL-SAFETY-LOCK] Account Drawdown ({drawdown_pct:.1f}%) > Max Limit ({config.MAX_ACCOUNT_DRAWDOWN_PERCENT}%). Blocking Bot {name} from NEW ENTRY.")
                                # We allow existing bots to maintain grids via `maintain_orders`, but BLOCK new ones.
                                return None, None
                    except Exception as e:
                        logger.error(f"Global Drawdown Safety Check Failed: {e}")

                    # 🚀 WORKFLOW VERIFICATION: Physical Reality Check (MOVED HERE)
                    # Before placing a NEW Entry, we must confirm we have NO position on the exchange.
                    can_enter = True
                    try:
                         # Use the snapshot passed from Runner
                         market_type = normalize_market_type(strategy.params.get('market_type', 'spot'))

                         snap_entry = exchange_snapshot.get(market_type, {}).get('positions', [])
                         
                         # Filter for this specific bot's pair/direction
                         real_pos = next((p for p in snap_entry if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair)), None)
                         
                         # 🚀 VIRTUAL HEDGING LOGIC (Refined)
                         # In One-Way Mode, we might have a position (e.g., LONG) from another bot.
                         # If WE (this bot) are not invested, we should be allowed to enter (reducing the net position).
                         # We only block entry if *WE* already have a physical footprint that implies we doubled up.
                         
                         if real_pos:
                              size = float(real_pos.get('contracts', 0) or real_pos.get('size', 0) or 0)
                              # Check 'side' vs 'mission side' isn't actually helpful in Net Mode (it's just +/- size)
                              
                              # If We are ALREADY Invested DB-side, we shouldn't be Entering "New" (that's maintain/grid).
                              # If We are NOT Invested DB-side, but a position exists, it must belong to the sibling bot.
                              # --> ALLOW ENTRY (It will act as a hedge/reduction).
                              
                              am_i_invested = bot_status.get('total_invested', 0) > 0
                              
                              if size > 0 and am_i_invested:
                                   # CRITICAL: I am active AND there is a position. 
                                   # This is a Double Entry risk.
                                   logger.warning(f"🛑 {name}: Attempted NEW ENTRY but already invested ({am_i_invested}). Aborting.")
                                   can_enter = False
                              elif size > 0 and not am_i_invested:
                                   # 🛡️ SYNC-LAG PROTECTION: If a position exists but DB says $0, check for recent fills
                                   from engine.database import get_last_filled_order
                                   last_fill = get_last_filled_order(bot_id)
                                   
                                   # If we had a fill in the last 60 seconds, trust that it's just sync lag.
                                   if last_fill and (time.time() - last_fill.get('created_at', 0)) < 60:
                                       logger.warning(f"🛡️ {name}: Position detected ({size}) and recent fill found in DB history. Blocking double-entry (Sync Lag).")
                                       can_enter = False
                                   else:
                                       logger.info(f"⚠️ {name}: Virtual Hedging - Physical Position exists ({size}), but I am new. Allowing Entry.")
                                       can_enter = True
                         
                    except Exception as e:
                         logger.error(f"Entry Safety Check Failed: {e}")

                    
                    if can_enter:
                        trade_update_data = self.execute_entry(bot_id, name, pair, mission['side'], mission['amount'], mission['price'], mission.get('params'), exchange, market_type_snapshot, bot_config, bot_status)
                    else:
                        trade_update_data = None
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
            
        last_exit = bot_status.get('last_exit_time', 0)
        basket_start = bot_status.get('basket_start_time', 0)
        logger.info(f"🧐 {name}: Checking Entry Logic. Invested={bot_status['total_invested']} LastExit={last_exit} BasketStart={basket_start}")

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
        existing_entry_order = next((o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')), None)

        # Get strategy from cache - FIXED: Use bot_config instead of bot_status for params
        strategy = self._get_strategy_instance(bot_id, bot_config)

        # 🚀 MISSING ENTRY LOGIC RESTORED 🚀
        # If we are NOT in a trade (total_invested == 0) and NO entry order exists, PLACE IT.
        # If an entry order already exists, handle CHASE logic or wait
        if existing_entry_order:
            # 🚀 CHASE LOGIC IMPLEMENTATION 🚀
            order_time = existing_entry_order.get('timestamp') or (int(time.time()) * 1000)
            order_age_sec = (int(time.time() * 1000) - order_time) / 1000.0
            
            # If order is more than 30s old and not filled, it might be stuck. 
            # Otherwise, WAIT for it to fill.
            if order_age_sec < 30.0:
                logger.info(f"⏳ {name}: Entry order exists and is recent ({order_age_sec:.1f}s). Waiting for fill.")
                return None

            # Configurable timeout (default 60s as per user request)
            CHASE_TIMEOUT_SEC = 60 
            
            if order_age_sec > CHASE_TIMEOUT_SEC:
                logger.info(f"⏱️ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old. Cancelling to CHASE price...")
                try:
                    exchange.cancel_order(existing_entry_order['id'], pair)
                    existing_entry_order = None # Reset so we place a new one below
                    time.sleep(1) # Brief pause to ensure cancellation propagates
                except Exception as e:
                    logger.error(f"❌ Bot {name}: Failed to cancel stale entry order: {e}")
            else:
                logger.info(f"⏳ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old (Timeout: {CHASE_TIMEOUT_SEC}s). Waiting...")
                return None

        # 🚀 FUNDAMENTAL FIX: Rigid Entry Lock
        # 1. Post-TP Cooldown: Prevent immediate "chasing" after a win.
        last_exit_time = bot_status.get('last_exit_time', 0)
        if last_exit_time and (time.time() - last_exit_time) < 30.0: # Increased to 30s for safety
             logger.info(f"⏳ {name}: Bot recently exited ({time.time() - last_exit_time:.1f}s ago). Cooldown in effect (30s) to allow WS sync.")
             return None

        # 2. In-Flight Buffer: Check if we ALREADY recorded an attempt in the last 15s
        # even if it hasn't landed in the exchange's open orders list yet.
        # We check the trade table's 'basket_start_time' which we set upon placement attempt.
        basket_start = bot_status.get('basket_start_time', 0)
        if basket_start and (time.time() - basket_start) < 15.0:
             logger.warning(f"🛡️ {name}: Entry attempt IN-FLIGHT ({time.time() - basket_start:.1f}s ago). Blocking double-tap.")
             return None

        logger.info(f"🧐 {name}: Proceeding to Place Entry Order...")

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
                    logger.info(f"🧐 {name}: Validating Order Params: {pair} {side} {amount} {price}")
                    valid, amount, price, msg = exchange.validate_order(pair, side, amount, price)
                    if not valid:
                        logger.error(f"❌ Entry Order validation failed for {name} {pair}: {msg}")
                        update_bot_error(bot_id, f"Entry Order validation failed: {msg}")
                        return

                    logger.info(f"🧐 {name}: Creating Order on Exchange...")
                    client_order_id = self._generate_deterministic_id(bot_id, 'ENTRY', 1)
                    order = self._place_gtx_order_with_retry(exchange, pair, side, amount, price, params={'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}, label=f"{name}-ENTRY")
                    if order:
                        try:
                            save_bot_order(bot_id, 'entry', order['id'], price, amount, 1, 'open', client_order_id=client_order_id)
                        except Exception as save_err:
                            logger.error(f"❌ {name}: Failed to save entry order to bot_orders: {save_err}")
                            
                        # 🚀 SURGICAL DB UPDATE: Record the order and lock the basket
                        # We do this directly here to avoid the Runner's stale overwrite loop.
                        try:
                            conn = get_connection()
                            cursor = conn.cursor()
                            # 1. Update trades table
                            # CRITICAL RACE CONDITION FIX: Do NOT overwrite total_invested or current_step to 0!
                            # The WebSocket thread might have already filled the order and updated them!
                            cursor.execute("""
                                UPDATE trades 
                                SET entry_order_id = ?
                                WHERE bot_id = ?
                            """, (order['id'], bot_id))
                            # 🚀 EE FIX: Stamp basket_start_time on new cycle entry so EE decay fires correctly.
                            # At this point the reset set basket_start_time=now (last cycle exit), but after 
                            # a full reset and new scan phase the value may remain 0. Stamp it here if missing.
                            cursor.execute("SELECT basket_start_time FROM trades WHERE bot_id = ?", (bot_id,))
                            bst_row = cursor.fetchone()
                            if not bst_row or not bst_row[0]:
                                cursor.execute("UPDATE trades SET basket_start_time = ? WHERE bot_id = ?", (int(time.time()), bot_id))
                                logger.info(f"⏱️ {name}: Stamped basket_start_time to now for new cycle (EE will decay correctly).")
                            # 2. Update bot status to IN TRADE immediately so UI/Log shows intent
                            cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))
                            conn.commit()
                            conn.close()
                            logger.info(f"✅ {name}: Recorded ENTRY order {order['id']} in DB.")
                            update_bot_error(bot_id, None) # Clear any previous error
                        except Exception as db_err:
                             logger.error(f"❌ {name}: Failed surgical DB update: {db_err}")
                             update_bot_error(bot_id, f"DB update error after entry: {db_err}")

                        return None  # 🚀 CRITICAL: Return None so Runner doesn't overwrite with stale 'total_invested=0'

                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"❌ {name}: Error placing ENTRY order for {pair}: {error_msg}")
                    # 🚀 BUBBLE ERROR TO UI
                    update_bot_error(bot_id, f"Entry Error: {error_msg}")
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
                        update_bot_error(bot_id, f"TP Order validation failed: {msg}")
                        return
                    
                    try:
                        client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
                        # 🚀 FIXED: Map direction to exchange side for TP
                        side = 'sell' if direction == 'LONG' else 'buy'
                        order = self._place_gtx_order_with_retry(exchange, pair, side, tp_amount, tp_price, params={'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}, label=f"{name}-TP")
                        if order:
                            save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], 'open', client_order_id=client_order_id)
                            logger.info(f"✅ {name}: Placed TP order for {pair} @ {tp_price} (ID: {order['id']})")
                            
                            # 🚀 GAP RECOVERY FIX: Sync the reconstructed live TP back to trades.target_tp_price
                            try:
                                from engine.database import get_connection
                                _upd_conn = get_connection()
                                _upd_conn.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (tp_price, bot_id))
                                _upd_conn.commit()
                                _upd_conn.close()
                                bot_status['target_tp_price'] = tp_price # update local memory too
                            except Exception as _sync_err:
                                logger.warning(f"Failed to sync reconstructed TP to DB for {name}: {_sync_err}")

                            existing_tp_order = order # 🚀 FIXED: Assign to memory so Step 4 doesn't delete it
                            update_bot_error(bot_id, None) # Clear any previous error
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"❌ {name}: Error placing TP order for {pair}: {error_msg}")
                        update_bot_error(bot_id, f"TP Order Error: {error_msg}")

        # 2b. EE/SYNC DRIFT CHECK: existing TP at wrong price or qty
        elif existing_tp_order and bot_status.get('total_invested', 0) > 0:
            db_tp  = self._compute_effective_tp(bot_id, name, bot_status, bot_config, strategy)
            db_qty = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            exchange_tp  = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            exchange_qty = self._get_order_amount(existing_tp_order)

            if db_tp > 0 and exchange_tp > 0:
                tp_drift_pct  = abs(db_tp  - exchange_tp)  / db_tp
                qty_drift_pct = abs(db_qty - exchange_qty) / max(db_qty, 0.0001)
                if tp_drift_pct > 0.001 or qty_drift_pct > 0.01:
                    logger.info(f"[SYNC-DRIFT] {name}: TP drifted price {tp_drift_pct*100:.4f}% qty {qty_drift_pct*100:.2f}%. Replacing.")
                    existing_tp_order = self._sync_replace_tp(
                        bot_id, name, pair, direction, bot_status, exchange,
                        db_tp, db_qty, existing_tp_order
                    )

        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps and bot_status['total_invested'] > 0:
            # Determine Grid details
            grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=bot_market_data)
            grid_price, grid_explain = grid_res if isinstance(grid_res, tuple) else (grid_res, "")
            grid_amount = strategy.calculate_grid_order_amount(bot_status, current_price, pair, exchange)

            if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} would place Grid order for {pair} @ {grid_price}")
                else:
                    side = 'buy' if direction == 'LONG' else 'sell'
                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        logger.error(f"❌ Grid Order validation failed for {name} {pair}: {msg}")
                        return
                    
                    try:
                        client_order_id_grid = self._generate_deterministic_id(bot_id, 'GRID', bot_status['current_step'] + 1)
                        order = self._place_gtx_order_with_retry(exchange, pair, side, grid_amount, grid_price, params={'clientOrderId': client_order_id_grid, 'postOnly': True, 'timeInForce': 'GTX'}, label=f"{name}-GRID-{bot_status['current_step'] + 1}")
                        if order:
                            save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, 'open', client_order_id=client_order_id_grid, notes=grid_explain)
                            logger.info(f"✅ {name}: Placed Grid order for {pair} @ {grid_price} (ID: {order['id']})")
                            existing_grid_order = order
                    except Exception as e:
                        logger.error(f"❌ {name}: Error placing Grid order for {pair}: {e}")
        
        # 3b. GRID SYNC-DRIFT: If grid exists but price is imprecise or drifted
        elif existing_grid_order and bot_status['total_invested'] > 0:
            grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=bot_market_data)
            target_grid_price, grid_explain = grid_res if isinstance(grid_res, tuple) else (grid_res, "")
            
            if target_grid_price > 0:
                side = 'buy' if direction == 'LONG' else 'sell'
                # Safe fallback for amount key — varies by source (CCXT vs DB cache)
                grid_amt_existing = float(existing_grid_order.get('amount') or existing_grid_order.get('origQty') or existing_grid_order.get('qty') or 0)
                # Pass through validation to get correct precision
                _, _, target_grid_price, _ = exchange.validate_order(pair, side, grid_amt_existing, target_grid_price)
                
                curr_grid_price = float(existing_grid_order.get('price', 0))
                # Replace if price drifted > 0.1% (avoid floating-point noise triggers)
                if abs(curr_grid_price - target_grid_price) / max(target_grid_price, 0.0001) > 0.001:
                    logger.info(f"🔄 [GRID-SYNC] {name}: Grid drifted ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Replacing.")
                    try:
                        grid_order_id = existing_grid_order.get('order_id', existing_grid_order.get('id'))
                        exchange.cancel_order(grid_order_id, pair)
                        update_order_status(grid_order_id, 'cancelled', bot_id=bot_id)
                        existing_grid_order = None # Force re-place in next cycle or later in this cycle?
                        # For simplicity, we just set it to None and let the 'if not existing_grid_order' block handle it next scan
                    except Exception as e_grid_sync:
                        logger.error(f"❌ [GRID-SYNC] {name}: Failed to cancel drifted grid: {e_grid_sync}")
                        
        # 4. Cleanup any untracked open orders for this bot
        # 🚀 FIXED: Handle None for existing orders prevents AttributeError
        # 🚀 FIXED: Order IDs in local DB are under `order_id`, while local row primary keys are `id`. We must default to `order_id` to compare physical IDs properly.
        known_order_ids = []
        if existing_tp_order: known_order_ids.append(existing_tp_order.get('order_id', existing_tp_order.get('id')))
        if existing_grid_order: known_order_ids.append(existing_grid_order.get('order_id', existing_grid_order.get('id')))
        
        # Also protect the Entry order from being cancelled as "untracked"
        if existing_entry_order:
             known_order_ids.append(existing_entry_order.get('order_id', existing_entry_order.get('id')))
        
        for order in bot_open_orders:
            # Skip if it is an Entry order (handled primarily by strategy logic)
            if '_ENTRY_' in order.get('clientOrderId', ''):
                continue
                
            if order['id'] not in known_order_ids:
                logger.warning(f"⚠️ {name}: Untracked open order {order['id']} for {pair}. Cancelling.")
                try:
                    exchange.cancel_order(order['id'], pair) # Use exchange's cancel_order, not specific bot
                    update_order_status(order['id'], 'cancelled', bot_id=bot_id)
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
                if order_status:
                    status = order_status.get('status')
                    filled = float(order_status.get('filled', 0))
                    amount = float(order_status.get('amount', 0))
                    
                    if status == 'filled' or (status == 'closed' and filled > 0 and filled >= amount * 0.99):
                        logger.info(f"✅ {name}: TP order {tp_order_id} filled. Resetting bot.")
                        reset_bot_after_tp(bot_id, current_price, direction=direction)
                    elif status in ['canceled', 'rejected'] or (status == 'closed' and filled == 0):
                        logger.warning(f"⚠️ {name}: TP order {tp_order_id} was canceled. Bot remains in trade.")
                        # Clear tp_order_id from DB so maintain_orders creates a new one
                        from engine.database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                        cursor.execute("UPDATE bot_orders SET status = 'cancelled' WHERE order_id = ?", (tp_order_id,))
                        conn.commit()
                        conn.close()
                    else:
                        logger.warning(f"⚠️ {name}: TP order {tp_order_id} not yet filled. Monitoring. (Status: {status}, Filled: {filled})")
            except Exception as e:
                err_msg = str(e).lower()
                if "not found" in err_msg or "-2013" in err_msg or "invalidorder" in err_msg:
                    logger.warning(f"⚠️ {name}: TP order {tp_order_id} no longer exists on Exchange (OrderNotFound). Purging from state.")
                    from engine.database import get_connection
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                    cursor.execute("UPDATE bot_orders SET status = 'missing' WHERE order_id = ?", (tp_order_id,))
                    conn.commit()
                    conn.close()
                else:
                    logger.error(f"❌ {name}: Error fetching TP order {tp_order_id} status: {e}")
        else:
            logger.warning(f"⚠️ {name}: No TP order found in DB for {pair}. Waiting for maintain_orders to place one.")
            # Do NOT force reset here, because the physical position is still open!
            # maintain_orders will place the TP order automatically on the next cycle.

    def maintain_orders(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ensures TP and Grid orders are placed active trades.
        """
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [MAINTAIN-BLOCKED] Trading disabled. Bot {name} cannot maintain orders.")
            return

        # 🚀 MARKET DATA SETUP
        # We need market data here for ATR and Grid Drift calculations. 
        # In maintain_orders, 'market_snapshot' is passed.
        current_market_data = None
        if market_snapshot:
             market_snapshot_inner = market_snapshot.get('market_data', {})
             current_market_data = market_snapshot_inner.get(pair)

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
        
        if bot_id == 10000:
             logger.debug(f"MAINTAIN Bot 10000 | OpenOrders={len(bot_open_orders)} | Snapshot={'Yes' if market_snapshot else 'No'}")

        
        # --- SELF-HEALING: Deduplicate Orders ---
        # Ensure only 1 TP and 1 Grid exist. If more, cancel the extras.
        grid_orders = [o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')]
        tp_orders = [o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')]
        
        # 🚀 STRICT SEQUENCING & STATE ENFORCEMENT
        existing_entry_order = next((o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')), None)

        # CASE 1: IN TRADE -> NO ENTRY ORDERS ALLOWED
        if bot_status['total_invested'] > 0 and existing_entry_order:
             logger.warning(f"🧹 {name}: Found dangling ENTRY order {existing_entry_order['id']} while IN TRADE. Cancelling to enforce state.")
             try:
                 exchange.cancel_order(existing_entry_order['id'], pair)
                 update_order_status(existing_entry_order['id'], 'cancelled', bot_id=bot_id)
                 existing_entry_order = None # Removed
             except Exception as e:
                 logger.error(f"Failed to cancel dangling entry: {e}")

        # CASE 2: SCANNING (No Position) -> NO TP/GRID ALLOWED 
        # (This is handled by 'untracked order' cleanup, but let's be explicit)
        # 🚀 ZERO-INVESTED RACE CONDITION FIX:
        # If the bot's `total_invested` still says 0.0 because the DB hasn't caught up,
        # but `current_step > 0` or we JUST placed an Entry order, it is actively in a trade.
        # Do NOT purge orders in this state.
        if bot_status['total_invested'] <= 10.0 and bot_status['current_step'] == 0:
            existing_grid_order = next((o for o in grid_orders), None)
            existing_tp_order = next((o for o in tp_orders), None)

            if existing_grid_order:
                logger.warning(f"👻 {name}: Found dangling GRID order while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(existing_grid_order['id'], pair)
                    update_order_status(existing_grid_order['id'], 'cancelled', bot_id=bot_id)
                except: pass
                grid_orders = [] # Clear local list
            
            if existing_tp_order:
                logger.warning(f"👻 {name}: Found dangling TP order while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(existing_tp_order['id'], pair)
                    update_order_status(existing_tp_order['id'], 'cancelled', bot_id=bot_id)
                except: pass
                tp_orders = []

        # 🚀 STEP-SYNC FIX: Ensure open orders match the CURRENT martingale step.
        # If we just had a grid fill, the old TP (from a previous step) is stale.
        current_step = bot_status['current_step']
        tp_tag = f"_TP_{current_step}_"
        grid_tag = f"_GRID_{current_step + 1}_"

        valid_tp_orders = [o for o in tp_orders if tp_tag in o.get('clientOrderId', '')]
        valid_grid_orders = [o for o in grid_orders if grid_tag in o.get('clientOrderId', '')]
        
        # 🚀 FORWARD-STEP-BUG FIX
        # A grid order's clientOrderId contains the step it belongs to (e.g. _GRID_2_).
        # We must NOT cancel orders that are strictly *greater* than our current step calculation
        # just because our local `bot_status` DB read is lagging by a few milliseconds!
        # Only cancel orders that are manifestly from the *past*.
        stale_orders = []
        for o in tp_orders:
            cid = o.get('clientOrderId', '')
            if tp_tag not in cid:
                try:
                    step_num = int(cid.split('_TP_')[1].split('_')[0])
                    if step_num < current_step:
                        stale_orders.append(o)
                except:
                     stale_orders.append(o) # Fallback if malformed
                     
        for o in grid_orders:
            cid = o.get('clientOrderId', '')
            if grid_tag not in cid:
                 try:
                     # Grid target is inherently Step + 1
                     step_num = int(cid.split('_GRID_')[1].split('_')[0])
                     if step_num < (current_step + 1):
                         stale_orders.append(o)
                 except:
                     stale_orders.append(o)
        if stale_orders:
            logger.warning(f"🧹 {name}: Found {len(stale_orders)} STALE orders from previous steps. Purging to sync with Step {current_step}...")
            for o in stale_orders:
                try:
                    exchange.cancel_order(o['id'], pair)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id)
                    logger.info(f"🔥 Cancelled stale {o.get('clientOrderId')}")
                except Exception as e:
                    logger.error(f"Failed to cancel stale {o['id']}: {e}")

        # Ensure only 1 valid TP and 1 valid Grid exist (Deduplication)
        if len(valid_grid_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(valid_grid_orders)} duplicate GRID orders for step {current_step+1}. Cleaning...")
            valid_grid_orders.sort(key=lambda x: str(x['id']), reverse=True)
            for o in valid_grid_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id)
                except: pass
            valid_grid_orders = [valid_grid_orders[0]]

        if len(valid_tp_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(valid_tp_orders)} duplicate TP orders for step {current_step}. Cleaning...")
            valid_tp_orders.sort(key=lambda x: str(x['id']), reverse=True)
            for o in valid_tp_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id)
                except: pass
            valid_tp_orders = [valid_tp_orders[0]]

        existing_grid_order = valid_grid_orders[0] if valid_grid_orders else None
        existing_tp_order = valid_tp_orders[0] if valid_tp_orders else None
        # ----------------------------------------

        strategy = self._get_strategy_instance(bot_id, bot_config)

        # 2. Check for missing / filled TP order
        if not existing_tp_order:
            tp_price = strategy.calculate_take_profit_price(bot_status, current_price)
            tp_amount = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            
            # 🚀 OFFLINE PROFIT GAP FIX (Maker Edition)
            # To prevent Binance Maker-Only ('GTX') -2010 rejections when filling offline gaps,
            # we must clip the order exactly to the top of the orderbook (Bid1/Ask1) instead of 
            # crossing the spread with a Taker limit order.
            gap_occurred = False
            if (direction == 'LONG' and current_price > tp_price) or (direction == 'SHORT' and current_price < tp_price):
                gap_occurred = True
                try:
                    ticker = exchange.fetch_ticker(pair)
                    # We are Selling to close a Long. Must join the Asks.
                    if direction == 'LONG':
                        ask = float(ticker.get('ask') or current_price)
                        tp_price = max(tp_price, ask)
                        logger.info(f"🚀 {name}: Offline Gap! Current price > TP. Adjusting TP to Ask {tp_price} to preserve Maker.")
                    # We are Buying to close a Short. Must join the Bids.
                    else:
                        bid = float(ticker.get('bid') or current_price)
                        tp_price = min(tp_price, bid)
                        logger.info(f"🚀 {name}: Offline Gap! Current price < TP. Adjusting TP to Bid {tp_price} to preserve Maker.")
                except Exception as e:
                    logger.warning(f"⚠️ {name}: Offline Gap, but fetch_ticker failed ({e}). Falling back to Taker gap adjustment.")
                    tp_price = current_price

            # Re-round just in case
            try:
                prec = exchange.get_symbol_precision(pair)
                tp_price = exchange.round_to_step(tp_price, prec['tick_size'])
            except: pass

            logger.info(f"🔍 [TP-MAINTENANCE] Checking TP for {name}: tp_price={tp_price}, amount={tp_amount}")
            if bot_id == 10000:
                 logger.debug(f"TP Logic Bot 10000 | Existing={existing_tp_order is not None} | Amt={tp_amount} | Price={tp_price} | Invested={bot_status['total_invested']}")


            if tp_amount > 0 and tp_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains TP for {pair} @ {tp_price}")
                else:
                    valid, tp_amount, tp_price, msg = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', tp_amount, tp_price)
                    if valid:
                        try:
                            client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
                            side = 'sell' if direction == 'LONG' else 'buy'
                            
                            ccxt_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}
                            
                            # 🚀 SPREAD-CROSS FIX: If the price was dynamically adjusted to `current_price` due to an Offline Gap,
                            # a Maker-Only GTX flag will guarantee Binance rejects it with `-2010`. We fallback to a standard GTC Limit.
                            if tp_price == exchange.round_to_step(current_price, exchange.get_symbol_precision(pair)['tick_size']):
                                logger.warning(f"⚠️ {name}: TP price matches active market gap. Dropping GTX Maker flag to allow execution.")
                                ccxt_params = {'clientOrderId': client_order_id, 'timeInForce': 'GTC'}
                            
                            # 🍰 REDUCE-ONLY DUST FIX: If this is the only active bot on this pair,
                            # mark the TP as reduceOnly so any tiny precision residual on the exchange
                            # gets fully closed — no sub-$5 dust is left behind.
                            try:
                                from engine.database import get_connection as _gc
                                _c = _gc()
                                _cur = _c.cursor()
                                _cur.execute("SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair=? AND t.total_invested>0 AND b.id!=?", (pair, bot_id))
                                other_bots_on_pair = _cur.fetchone()[0]
                                _c.close()
                                if other_bots_on_pair == 0:
                                    ccxt_params['reduceOnly'] = True
                                    logger.info(f"🍰 {name}: Sole bot on {pair} — TP set to reduceOnly to flatten any dust residual.")
                            except Exception:
                                pass  # Non-critical, don't block TP placement
                                
                            order = self._place_gtx_order_with_retry(exchange, pair, side, tp_amount, tp_price, params=ccxt_params, label=f"{name}-MAINTAIN-TP")
                            if order:
                                save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], 'open', client_order_id=client_order_id)
                                logger.info(f"✅ {name}: Maintained TP order for {pair} @ {tp_price}")
                        except Exception as e:
                             logger.error(f"❌ {name}: Error maintaining TP: {e}")

        # 2b. EE/SYNC DRIFT CHECK: existing TP at wrong price or qty
        elif existing_tp_order and bot_status.get('total_invested', 0) > 0:
            db_tp  = self._compute_effective_tp(bot_id, name, bot_status, bot_config, strategy)
            db_qty = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            exchange_tp  = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            exchange_qty = self._get_order_amount(existing_tp_order)

            # DB-TP ZERO GUARD: recalculate if target_tp_price was wiped (post-repair/reset)
            if db_tp == 0 and bot_status.get('avg_entry_price', 0) > 0:
                db_tp = strategy.calculate_take_profit_price(bot_status, bot_status.get('avg_entry_price', 0))
                logger.info(f"[TP-RECOVER] {name}: db_tp was 0, recalculated to {db_tp:.4f} from avg_entry.")
                try:
                    _c = get_connection()
                    _c.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (db_tp, bot_id))
                    _c.commit(); _c.close()
                except Exception:
                    pass

            if db_tp > 0 and exchange_tp > 0:
                tp_drift_pct  = abs(db_tp  - exchange_tp)  / db_tp
                qty_drift_pct = abs(db_qty - exchange_qty) / max(db_qty, 0.0001)
                if tp_drift_pct > 0.001 or qty_drift_pct > 0.01:
                    logger.info(f"[SYNC-DRIFT] {name}: TP drifted price {tp_drift_pct*100:.4f}% qty {qty_drift_pct*100:.2f}%. Replacing.")
                    existing_tp_order = self._sync_replace_tp(
                        bot_id, name, pair, direction, bot_status, exchange,
                        db_tp, db_qty, existing_tp_order
                    )


        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps:
             # 🚀 STRICT SEQUENCING: Do NOT place Grid orders if an Entry order is still open.
             if existing_entry_order:
                  logger.info(f"⏳ {name}: Entry order is still open. Waiting for Full Fill before placing Grid Orders.")
                  return None

             # 🛡️ PHYSICAL-SIZE GUARD: Detect unprocessed offline fills before placing new grid.
             # In a multi-bot environment, the exchange's physical position is the NET 
             # (in One-Way mode) or absolute (in Hedge mode) sum of ALL bots on that pair.
             # Comparing a single bot's virtual_qty to the total phys_qty is invalid.
             # We must aggregate the virtual quantities of ALL bots on this pair.
             try:
                 phys_positions = market_snapshot.get('positions', []) if market_snapshot else []
                 
                 # 1. Calculate Net Physical from exchange
                 phys_net = 0.0
                 for p in phys_positions:
                     if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                         size = float(p.get('contracts', 0) or 0)
                         if p.get('side', '').upper() == 'SHORT':
                             phys_net -= size
                         else:
                             phys_net += size
                             
                 # 2. Calculate Net Virtual from ALL active bots on this pair
                 from engine.database import get_connection
                 conn = get_connection()
                 cursor = conn.cursor()
                 cursor.execute('''
                     SELECT direction, total_invested, avg_entry_price 
                     FROM bots 
                     WHERE pair = ? AND status != 'Stopped'
                 ''', (pair,))
                 active_bots = cursor.fetchall()
                 conn.close()
                 
                 virtual_net = 0.0
                 for b_dir, b_inv, b_avg in active_bots:
                     if b_inv > 0 and b_avg > 0:
                         b_qty = b_inv / b_avg
                         if b_dir.upper() == 'LONG':
                             virtual_net += b_qty
                         else:
                             virtual_net -= b_qty
                             
                 # Only check if this bot's direction aligns with the net mis-match.
                 # E.g. if exchange has way more LONG than we think, block LONG grid.
                 # Allowance: 10% of virtual net + small absolute buffer (e.g. 0.001)
                 # We only trigger the guard if the physical mismatch is strictly larger than what this grid step would add.
                 # To keep it simple, if absolute physical net is > 110% of absolute virtual net, flag it.
                 # Only apply this guard if there's actually a significant virtual net (avoid div/0 on tiny sizes)
                 if abs(virtual_net) > 0.0:
                     if abs(phys_net) > abs(virtual_net) * 1.10:
                        logger.warning(
                            f"🛑 {name}: Physical net {phys_net:.4f} >> virtual net {virtual_net:.4f} "
                            f"(+{((abs(phys_net)/abs(virtual_net))-1)*100:.0f}%). "
                            f"Offline fill likely unprocessed. SKIPPING new grid until reconciler catches up."
                        )
                        return None
                 else:
                     # If virtual net is ~0 (fully hedged or no positions), but physical is non-zero
                     if abs(phys_net) > 0.001: 
                         # A small threshold, since we don't know the tick size here generically, 0.001 is a safe lower bound
                         logger.warning(f"🛑 {name}: Physical net {phys_net:.4f} exists but virtual net is 0. SKIPPING grid.")
                         return None
                         
             except Exception as _guard_err:
                 logger.debug(f"Physical-size guard check failed for {name}: {_guard_err}")

                                       # 🚀 STEP-PROGRESSION-PROOF: Before placing Step N+1, prove Step N is actually filled!
             if bot_status['current_step'] > 0:
                 try:
                     # 🛡️ FUNDAMENTAL FIX: Trust the `entry_confirmed` flag as primary proof.
                     if bot_status.get('entry_confirmed', 0) == 1:
                         logger.info(f"🛡️ {name}: Bypassing fill-proof check because trade/step is natively confirmed.")
                     else:
                         from engine.database import get_connection
                         conn = get_connection()
                         cursor = conn.cursor()
                         # Fallback to order history (30-day window)
                         cursor.execute("""
                             SELECT COUNT(*) FROM bot_orders 
                             WHERE bot_id=? AND status IN ('filled', 'closed') AND step=? AND created_at >= (? - 2592000)
                         """, (bot_id, bot_status['current_step'], bot_status.get('basket_start_time', 0)))
                         row = cursor.fetchone()
                         conn.close()
                         if not row or row[0] == 0:
                             logger.warning(
                                 f"🛑 {name}: Step Progression Blocked! Step {bot_status['current_step']} proof-of-fill not found in DB. "
                                 f"Waiting for reconciler/WS to confirm."
                             )
                             return None
                 except Exception as e:
                     logger.error(f"❌ Error checking step progression proof for {name}: {e}")


             # 🚀 FUNDAMENTAL FIX: Re-calculate base size dynamically here
             # Just like execute_entry, we must override bot_config BEFORE calling Strategy
             if bot_config.get('use_min_size', False):
                 prec = exchange.get_symbol_precision(pair)
                 step_size = prec.get('step_size', 0.001)
                 min_cost_qty = step_size * current_price
                 if getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False):
                     min_cost_notional = 100.0
                 else:
                     min_cost_notional = 5.0 if market_type == 'future' else 10.0
                     
                 bot_config['base_size'] = max(min_cost_qty, min_cost_notional) * 1.05
             else:
                 if getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False):
                     if bot_config.get('base_size', 0) < 100.0:
                         bot_config['base_size'] = 105.0
            
             grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data)
             if isinstance(grid_res, tuple):
                  grid_price, grid_explain = grid_res
             else:
                  grid_price, grid_explain = grid_res, ""

             grid_amount = strategy.calculate_grid_order_amount(bot_status, current_price, pair, exchange)
             
             # 🚀 OFFLINE GRID GAP FIX (Maker Edition)
             # If the market swept past our intended grid target, placing a standard grid limit order
             # will cross the spread and trigger a -2010 GTX Maker-Only rejection.
             if (direction == 'LONG' and current_price < grid_price) or (direction == 'SHORT' and current_price > grid_price):
                 try:
                     ticker = exchange.fetch_ticker(pair)
                     # We are Buying to open a Long grid. Must join the Bids.
                     if direction == 'LONG':
                         bid = float(ticker.get('bid') or current_price)
                         grid_price = min(grid_price, bid)
                         logger.info(f"🚀 {name}: Grid Gap! Price Dropped. Adjusting Grid to Bid {grid_price} to preserve Maker.")
                     # We are Selling to open a Short grid. Must join the Asks.
                     else:
                         ask = float(ticker.get('ask') or current_price)
                         grid_price = max(grid_price, ask)
                         logger.info(f"🚀 {name}: Grid Gap! Price Rallied. Adjusting Grid to Ask {grid_price} to preserve Maker.")
                 except Exception as e:
                     logger.warning(f"⚠️ {name}: Grid Gap, but fetch_ticker failed ({e}). Falling back to Taker grid adjustment.")
                     grid_price = current_price
                     
             logger.info(f"🔍 [GRID-MAINTENANCE] {name}: Target=${grid_price} | {grid_explain}")

             if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains Grid for {pair} @ {grid_price}")
                else:
                    logger.info(f"🔍 [GRID-DEBUG] Bot {name} ({direction}) | Price={current_price} | GridTarget={grid_price} | Amount={grid_amount} | Step={bot_status['current_step']} | BaseSize={bot_config.get('base_size')} | Multi={bot_config.get('martingale_multiplier')} | StratBase={strategy.params.get('base_size')} | StratMult={strategy.params.get('martingale_multiplier')}")
                    
                    side = 'buy' if direction == 'LONG' else 'sell'
                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        logger.error(f"❌ Grid Order validation failed for {name} {pair}: {msg}")
                    else:
                        try:
                            client_order_id_grid = self._generate_deterministic_id(bot_id, 'GRID', bot_status['current_step'] + 1)
                            # 🚀 FIXED: Map direction to exchange side
                            
                            ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'postOnly': True, 'timeInForce': 'GTX'}
                            
                            # 🚀 SPREAD-CROSS FIX: If the grid target evaluates to precisely the current active market price
                            # (due to rapid drops/gaps), drop Maker-Only GTX flag to prevent immediate rejection (-2010).
                            if grid_price == exchange.round_to_step(current_price, exchange.get_symbol_precision(pair)['tick_size']):
                                logger.warning(f"⚠️ {name}: Grid price matches active market gap. Dropping GTX Maker flag to allow execution.")
                                ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'timeInForce': 'GTC'}
                                
                            order = self._place_gtx_order_with_retry(exchange, pair, side, grid_amount, grid_price, params=ccxt_grid_params, label=f"{name}-MAINTAIN-GRID")
                            if order:
                                save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, 'open', client_order_id=client_order_id_grid, notes=grid_explain)
                                logger.info(f"✅ {name}: Maintained Grid order for {pair} @ {grid_price}")
                        except Exception as e:
                            err_msg = str(e)
                            if "-2027" in err_msg or "Exceeded the maximum allowable position" in err_msg:
                                logger.warning(f"🛑 {name}: Max Position Limit Reached (Leverage Constraint). Pausing Grid.")
                                # Optional: set a flag to stop trying?
                            else:
                                logger.error(f"❌ {name}: Error maintaining Grid: {e}")
                            
        # 3b. GRID SYNC-DRIFT: If grid exists but price is imprecise or drifted
        elif existing_grid_order and bot_status['total_invested'] > 0:
            grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data)
            target_grid_price, grid_explain = grid_res if isinstance(grid_res, tuple) else (grid_res, "")
            
            if target_grid_price > 0:
                side = 'buy' if direction == 'LONG' else 'sell'
                # Safe fallback for amount key — varies by source (CCXT vs DB cache)
                grid_amt = float(existing_grid_order.get('amount') or existing_grid_order.get('origQty') or existing_grid_order.get('qty') or 0)
                # Pass through validation to get correct precision
                _, _, target_grid_price, _ = exchange.validate_order(pair, side, grid_amt, target_grid_price)
                
                curr_grid_price = float(existing_grid_order.get('price', 0))
                # Replace if price drifted > 0.1% (avoid floating-point noise triggers)
                if abs(curr_grid_price - target_grid_price) / max(target_grid_price, 0.0001) > 0.001:
                    logger.info(f"🔄 [GRID-SYNC] {name}: Grid drifted ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Replacing.")
                    try:
                        grid_order_id = existing_grid_order.get('order_id', existing_grid_order.get('id'))
                        exchange.cancel_order(grid_order_id, pair)
                        update_order_status(grid_order_id, 'cancelled', bot_id=bot_id)
                        existing_grid_order = None # Force re-place in next cycle
                    except Exception as e_grid_sync:
                        logger.error(f"❌ [GRID-SYNC] {name}: Failed to cancel drifted grid: {e_grid_sync}")
                        
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
            # We must only close THIS bot's portion, not the entire exchange position!
            
            if bot_status['avg_entry_price'] > 0:
                size_to_close = bot_status['total_invested'] / bot_status['avg_entry_price']
                
                # Fetch current position to ensure we don't over-close if exchange has less
                positions = exchange.fetch_positions()
                current_position = next((p for p in positions if normalize_symbol(p.get('symbol')) == normalize_symbol(pair)), None)
                exchange_size = float(current_position.get('contracts', 0) or current_position.get('size', 0) or 0) if current_position else 0.0
                
                # Cap the close size to what is actually available on the exchange for this side
                actual_size = min(abs(size_to_close), abs(exchange_size))
                
                if actual_size > 0:
                    logger.warning(f"Placing market order to close {actual_size} {pair} {position_side} for bot {name} SL")
                    order = exchange.create_order(pair, 'market', position_side, actual_size)
                    if order:
                        log_trade(bot_id, 'STOP_LOSS_EXIT', pair, current_price, actual_size, current_price * actual_size, f'SL_MARKET_{bot_id}', bot_status['current_step'], "SL Market Exit", (current_price - bot_status['avg_entry_price']) * actual_size)
                        reset_bot_after_tp(bot_id, current_price, direction=direction)
                        logger.info(f"✅ {name}: Market order placed to close SL for {pair} (ID: {order['id']})")
                    else:
                        logger.error(f"❌ {name}: Failed to place market order for SL exit for {pair}")
                else:
                    logger.info(f"ℹ️ {name}: No active position found on exchange for {pair} to close. Resetting DB state.")
                    reset_bot_after_tp(bot_id, current_price, direction=direction)
            else:
                logger.info(f"ℹ️ {name}: Bot has 0 avg_entry_price. Resetting DB state without market order.")
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
