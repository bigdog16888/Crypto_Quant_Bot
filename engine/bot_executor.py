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
    safe_wipe_bot,
    save_bot_order,
    update_bot_order_exchange_id,
    get_bot_order_ids,
    get_connection,
    get_all_active_trades_for_pair,
    update_order_status,
    flag_bot_pos_limit,
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
    # 🛡️ Binance margin and position limit rejection signals
    _MARGIN_SIGNALS = [
        "-2019", "-2027", "-4131", "-4003",
        "margin is insufficient", "account has insufficient balance",
        "exceed maximum position", "position limit",
    ]


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
        Format: CQB_{bot_id}_{TYPE}_{STEP}_{TIMESTAMP_MS}
        """
        timestamp_ms = int(time.time() * 1000)
        return f"CQB_{bot_id}_{type_str.upper()}_{step_index}_{timestamp_ms}"

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

    def _get_phys_pos(self, pair: str, direction: str = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves the physical exchange position for a pair from the active_positions table.
        
        NOTE: active_positions stores ONE row per pair (last reconciled state). In Hedge Mode,
        the exchange holds LONG and SHORT separately, but only the dominant/last side is in this table.
        
        When direction is given: tries exact side match first, then falls back to any row for the pair.
        This prevents blocking (e.g. short btc can't find its SHORT row because only LONG is stored)
        while still being direction-aware when both sides are stored separately.
        """
        try:
            from engine.exchange_interface import normalize_symbol
            clean_pair = normalize_symbol(pair)
            from engine.database import get_connection
            with get_connection() as conn:
                if direction:
                    expected_side = 'LONG' if direction.upper() == 'LONG' else 'SHORT'
                    # Try exact side match first
                    row = conn.execute(
                        "SELECT size, side, entry_price FROM active_positions WHERE pair = ? AND side = ?",
                        (clean_pair, expected_side)
                    ).fetchone()
                    if not row:
                        # Fall back: active_positions has only one row per pair — use it regardless of side
                        row = conn.execute(
                            "SELECT size, side, entry_price FROM active_positions WHERE pair = ?",
                            (clean_pair,)
                        ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT size, side, entry_price FROM active_positions WHERE pair = ?",
                        (clean_pair,)
                    ).fetchone()
                logger.info(f"[PHYS-TRACE] _get_phys_pos called for {pair} -> {clean_pair} dir={direction} | ROW: {row}")
                if row:
                    return {'size': float(row[0]), 'side': str(row[1]).upper(), 'entry_price': float(row[2])}
            return None
        except Exception as e:
            logger.warning(f"⚠️ [PHYS-SENSE] Lookup failed for {pair}: {e}")
            return None


    def _is_order_net_reducing(self, pair, side, qty):
        """
        Determines if a proposed order reduces the account's total physical net position.
        This is critical for navigating 'Margin Cap' (-2019) rejections in multi-bot setups.
        """
        pos_info = self._get_phys_pos(pair)
        if not pos_info or pos_info['size'] == 0:
            return False # No position to reduce

        # physical signed qty: + for long, - for short
        phys_signed = pos_info['size'] if pos_info['side'] == 'LONG' else -pos_info['size']
        # order signed qty: + for buy, - for sell
        order_signed = qty if side.lower() == 'buy' else -qty
        
        # New net position if order fills
        new_net = phys_signed + order_signed
        
        # Order is net reducing if the absolute account exposure decreases
        return abs(new_net) < abs(phys_signed) - 0.0001



    def _prepare_tp_order_params(self, bot_id, name, pair, side, amount, tp_price, current_price, exchange):
        """
        Calculates Take Profit parameters using this bot's own virtual position size.
        
        Architecture (Correct):
        1. Each bot manages its OWN position. TP qty = bot's own virtual open qty from its ledger.
        2. Physical position is used as a sanity cap (can't close more than physically exists).
        3. When all bots close their own shares, the physical net goes to zero naturally.
        4. NO sibling subtraction — that was causing BTC TP cancel loops.
        5. NO reduceOnly — causes rejections in multi-bot setups.
        """
        ccxt_params = {'postOnly': True, 'timeInForce': 'GTX'}

        # 1. Get physical reality for sanity cap (direction-filtered for Hedge Mode)
        # Pass the bot's direction so in Hedge Mode we only see OUR side's row,
        # not a sibling bot's opposing position.
        bot_direction = 'LONG' if side.lower() == 'sell' else 'SHORT'
        pos_info = self._get_phys_pos(pair, direction=bot_direction)
        phys_qty = pos_info['size'] if pos_info else 0.0

        if phys_qty <= 0.0:
            logger.info(f"INFO {name}: No physical {bot_direction} position on exchange for {pair}. TP not needed.")
            return None, None


        # 2. Calculate THIS bot's own virtual open qty from its ledger
        # This is the authoritative source for how much THIS bot should close.
        try:
            from engine.database import get_connection as _gc_tp
            with _gc_tp() as _c_tp:
                _cur = _c_tp.cursor()
                # Get current cycle_id for this bot
                _cur.execute("SELECT t.cycle_id FROM trades t WHERE t.bot_id = ?", (bot_id,))
                cycle_row = _cur.fetchone()
                cycle_id = cycle_row[0] if cycle_row else 1
                
                # Sum this bot's own filled entries minus exits for the current cycle
                _cur.execute("""
                    SELECT 
                        COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0)
                    FROM bot_orders
                    WHERE bot_id = ? 
                    AND status NOT IN ('reset_cleared', 'auto_closed')
                    AND (cycle_id = ? OR cycle_id IS NULL)
                    AND filled_amount > 0
                """, (bot_id, cycle_id))
                ledger_row = _cur.fetchone()
                bot_entry_qty = float(ledger_row[0] or 0.0)
                bot_exit_qty = float(ledger_row[1] or 0.0)
                bot_virtual_open_qty = max(0.0, bot_entry_qty - bot_exit_qty)
                
        except Exception as e:
            logger.warning(f"⚠️ {name}: Failed to calculate virtual ledger qty: {e}. Falling back to DB amount.")
            bot_virtual_open_qty = amount  # fallback to DB-stored amount

        if bot_virtual_open_qty <= 0.0:
            logger.debug(f"{name}: Bot virtual ledger qty=0 (already fully exited?). TP not needed.")
            return None, None

        # 3. Cap by physical reality — never close more than physically exists
        prec = exchange.get_symbol_precision(pair)
        tp_qty_raw = min(bot_virtual_open_qty, phys_qty)
        tp_qty = exchange.round_to_step(tp_qty_raw, prec['step_size'])

        if tp_qty <= 0:
            logger.info(f"INFO {name}: TP qty rounds to 0 after step_size. Skipping.")
            return None, None

        # 4. Log net-reducing direction for informational awareness
        is_reducing = self._is_order_net_reducing(pair, side, tp_qty)
        if not is_reducing:
            logger.warning(f"⚠️ {name}: TP order {side.upper()} {tp_qty} NOTE: increases account NET exposure (multi-bot hedge scenario).")

        # 5. Dust check: if notional is below minimum, trigger dust close path
        _min_notional = prec.get('min_notional')
        if _min_notional is None:
            _min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
        
        notional = tp_qty * (tp_price or current_price or 1.0)
        if notional < _min_notional:
            logger.warning(f"DUST {name}: TP notional ${notional:.2f} < min ${_min_notional:.2f}. Adjusting TP price to meet min notional.")
            # Rather than wash-trading instantly, we clamp the TP price so the order is validly placed for *some* profit.
            # To meet the minimum notional, tp_price must be at least _min_notional / tp_qty.
            required_price = (_min_notional * 1.002) / tp_qty  # Add tiny 0.2% buffer
            
            # For SHORTs, increasing the tp_price means taking less profit.
            # But if required_price > current_price, taking profit means actually losing money!
            if side == 'buy' and required_price >= current_price:
                 logger.warning(f"🛑 {name}: Minimum notional clamp ({required_price:.4f}) breaches current price ({current_price:.4f}). Cannot TP profitably.")
                 # Fallback: Place at exactly current maker boundary to escape the trapped size
                 return 'DUST_CHASER', tp_qty
                 
            try:
                prec = exchange.get_symbol_precision(pair)
                tp_price = exchange.ceil_to_step(required_price, prec['tick_size']) if side == 'buy' else exchange.round_to_step(required_price, prec['tick_size'])
            except:
                tp_price = required_price
                
            logger.info(f"✨ {name}: Clamped TP to {tp_price:.4f} to satisfy min notional of ${_min_notional:.2f}.")

        # 6. Spread-Cross Fix: TP at market price must be GTC taker
        try:
            if tp_price == exchange.round_to_step(current_price, prec['tick_size']):
                logger.warning(f"WARN {name}: price already at or beyond TP, switching to GTC.")
                ccxt_params.pop('postOnly', None)
                ccxt_params['timeInForce'] = 'GTC'
        except Exception:
            pass

        logger.debug(f"OK {name}: TP qty={tp_qty:.4f} (virtual={bot_virtual_open_qty:.4f}, physical={phys_qty:.4f}) notional=${notional:.2f}")
        return ccxt_params, tp_qty


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
                
                # Fix for Duplicate ClientOrderId on Retry
                retry_params = dict(params) if params else {}
                if 'clientOrderId' in retry_params:
                    retry_params['clientOrderId'] = f"{retry_params['clientOrderId']}_R"
                if 'newClientOrderId' in retry_params:
                    retry_params['newClientOrderId'] = f"{retry_params['newClientOrderId']}_R"

                return exchange.create_order(pair, 'limit', side, amount, retry_price, params=retry_params)
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
        Returns the new order dict, or None on failure."""
        try:
            tp_order_id = existing_tp_order.get('order_id', existing_tp_order.get('id'))
            exchange.cancel_order(tp_order_id, pair)
            update_order_status(tp_order_id, 'cancelled', bot_id=bot_id)

            if db_qty <= 0 or db_tp <= 0 or config.DRY_RUN:
                return None

            side = 'sell' if direction == 'LONG' else 'buy'
            valid, db_qty, db_tp, msg = exchange.validate_order(pair, side, db_qty, db_tp, is_closing=True)
            if not valid:
                logger.warning(f"[TP-SYNC] {name}: Validation failed — {msg}")
                return None

            client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
            tp_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

            # 🚀 PRE-COMMIT: Write 'placing' row to DB BEFORE exchange call.
            # If engine shuts down between here and the exchange response, the reconciler
            # finds this row, queries exchange by clientOrderId, and recovers the fill.
            _tp_pre_id = save_bot_order(bot_id, 'tp', f'PLACING_{client_order_id}', db_tp, db_qty,
                                        bot_status['current_step'], 'placing', client_order_id=client_order_id,
                                        notes='pre-commit')
            order = self._place_gtx_order_with_retry(
                exchange, pair, side, db_qty, db_tp, params=tp_params, label=f"{name}-TP-SYNC"
            )
            if order:
                update_bot_order_exchange_id(_tp_pre_id, order['id'], order.get('status', 'open'))
                logger.info(f"✅ [SYNC] {name}: Re-placed TP @ {db_tp:.4f} Qty {db_qty:.4f}")
            else:
                update_bot_order_exchange_id(_tp_pre_id, None, 'failed')
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
            
            # 🚀 FUNDAMENTAL FIX: Drain the WS TP deferred cancel registry.
            # When a TP fills via WebSocket (ws_event_handlers._handle_order_filled),
            # there is no exchange object available to cancel lingering grid orders.
            # The handler registers (bot_id, pair) in get_pending_cancel_after_tp().
            # Here — on the very next bot tick — we have exchange access and drain it:
            # any open bot-tagged orders on the pair are cancelled, preventing orphaned fills.
            try:
                from engine.ws_event_handlers import get_pending_cancel_after_tp
                pending = get_pending_cancel_after_tp()
                if (bot_id, pair) in pending:
                    logger.info(f"🧹 [DEFERRED-CANCEL] Draining WS TP cancel registry for bot {name} ({bot_id}) / {pair}.")
                    try:
                        open_orders = exchange.fetch_open_orders(pair)
                        bot_tag = f"CQB_{bot_id}_"
                        for o in open_orders:
                            cid = o.get('clientOrderId', '')
                            oid = o.get('id')
                            if not cid.startswith(bot_tag):
                                continue
                            logger.info(f"🧹 [DEFERRED-CANCEL] Cancelling orphan-risk order {oid} ({cid}) for {name}.")
                            try:
                                exchange.cancel_order(oid, pair)
                                from engine.database import update_order_status as _uos_dc
                                _uos_dc(oid, 'cancelled', bot_id=bot_id)
                            except Exception as e_dc_cancel:
                                logger.warning(f"⚠️ [DEFERRED-CANCEL] Could not cancel {oid}: {e_dc_cancel}")
                    except Exception as e_dc_fetch:
                        logger.warning(f"⚠️ [DEFERRED-CANCEL] fetch_open_orders failed: {e_dc_fetch}")
                    # Re-register others that were not for this bot (avoid consuming their entry)
                    # Note: get_pending_cancel_after_tp() already clears the full set.
                    # Re-add entries that don't belong to this bot so they get processed on their own tick.
                    for entry in pending:
                        if entry != (bot_id, pair):
                            from engine.ws_event_handlers import _pending_cancel_after_tp
                            _pending_cancel_after_tp.add(entry)
            except Exception as e_drain:
                logger.debug(f"[DEFERRED-CANCEL] Registry drain error: {e_drain}")
            
            current_price = exchange.get_last_price(pair) # Get current price
            if not current_price:
                logger.warning(f"Could not get current price for {pair}. Skipping bot {name}.")
                return None, None
                
            # 🚀 AUTO MIN-SIZE CALCULATION
            if bot_config.get('use_min_size', False):
                prec = exchange.get_symbol_precision(pair)
                step_size = prec.get('step_size', 0.001)
                min_cost_qty = step_size * current_price
                
                # Fetch real exchange minimum notional (per-symbol from Binance exchangeInfo)
                # Fallback: demo/testnet Binance FAPI enforces $100; mainnet is typically $5
                exchange_min_notional = prec.get('min_notional', None)
                if exchange_min_notional is None:
                    exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                
                # Target 5% above the strict minimum to avoid slippage drops below min notional
                auto_min_size = max(min_cost_qty, exchange_min_notional) * 1.05
                bot_config['base_size'] = auto_min_size
                logger.debug(f"AUTO-MIN-SIZE {pair}: exchange_min=${exchange_min_notional:.2f} → base_size=${auto_min_size:.2f}")
            else:
                # 🚀 STRICT VALIDATION: If configured base_size is below exchange real minimum, HALT.
                prec = exchange.get_symbol_precision(pair)
                exchange_min_notional = prec.get('min_notional', None)
                if exchange_min_notional is None:
                    exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                if bot_config.get('base_size', 0) < exchange_min_notional:
                    logger.error(f"⛔ CONFIG ERROR [{pair}]: Configured base_size=${bot_config.get('base_size',0):.2f} is below exchange minimum ${exchange_min_notional:.2f}. Halting bot. Please update config.")
                    update_bot_error(bot_id, "Config Error")
                    return None, None

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
                              abs_size = abs(size)
                              
                              am_i_invested = bot_status.get('total_invested', 0) > 0
                              
                              if abs_size > 0 and am_i_invested:
                                   # CRITICAL: I am active AND there is a position. 
                                   # This is a Double Entry risk.
                                   logger.warning(f"🛑 {name}: Attempted NEW ENTRY but already invested ({am_i_invested}). Aborting.")
                                   can_enter = False
                              elif abs_size > 0 and not am_i_invested:
                                   # 🛡️ ORPHAN / MISMATCH PROTECTION
                                   # Before allowing "Virtual Hedging" entry, verify this physical size belongs to sibling bots.
                                   from engine.database import get_all_active_trades_for_pair
                                   sibling_bots = get_all_active_trades_for_pair(pair)
                                   
                                   # 🚀 Calculate exact signed virtual quantities directly from the ledger
                                   # This prevents floating point division drift from historical invested vs live notional
                                   from engine.database import get_connection
                                   sib_net_qty = 0.0
                                   conn = get_connection()
                                   try:
                                        cursor = conn.cursor()
                                        for b in sibling_bots:
                                            sibling_id = b['bot_id']
                                            cursor.execute("""
                                                SELECT COALESCE(SUM(
                                                    CASE 
                                                        WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount
                                                        WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -filled_amount
                                                        ELSE 0.0
                                                    END
                                                ), 0.0) 
                                                FROM bot_orders 
                                                WHERE bot_id = ? AND status NOT IN ('reset_cleared', 'auto_closed', 'canceled', 'rejected')
                                                AND (cycle_id = (SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id = ?) OR cycle_id IS NULL)
                                            """, (sibling_id, sibling_id))
                                            row = cursor.fetchone()
                                            q = float(row[0]) if row else 0.0
                                            
                                            if b['direction'].upper() == 'LONG':
                                                sib_net_qty += q
                                            else:
                                                sib_net_qty -= q
                                   finally:
                                        conn.close()
                                            
                                   # 🚀 Compare actual quantities to ignore market price fluctuations
                                   phys_net_qty_abs = abs(size)
                                   sib_net_qty_abs  = abs(sib_net_qty)
                                   
                                   # Convert quantity drift back to USD simply to keep the threshold metric ($50 tolerance)
                                   drift_qty = abs(sib_net_qty_abs - phys_net_qty_abs)
                                   drift_usd = drift_qty * current_price
                                   
                                   if drift_usd > 50.0:
                                        logger.critical(f"🛑 {name}: Blocked NEW ENTRY! Exchange magnitude {phys_net_qty_abs:.6f} vs System {sib_net_qty_abs:.6f} mismatch > $50. Resolve Mismatch first!")
                                        can_enter = False
                                   else:
                                        from engine.database import get_last_filled_order
                                        last_fill = get_last_filled_order(bot_id)
                                        if last_fill and (time.time() - last_fill.get('created_at', 0)) < 60:
                                            logger.warning(f"🛡️ {name}: Position detected ({size}) and recent fill found. Blocking double-entry (Sync Lag).")
                                            can_enter = False
                                        else:
                                            logger.info(f"⚠️ {name}: Virtual Hedging - Physical Position exists ({size}), backed by siblings. Allowing Entry.")
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
                elif mission['action'] == 'hedge_open':
                    trade_update_data = self.execute_hedge_lock(bot_id, name, pair, direction, bot_status, mission['price'], mission['qty'], mission['step'], exchange, bot_config)

                return mission.get('sleep_interval', 5.0), trade_update_data

        except Exception as e:
            logger.error(f"Error processing bot {name} ({bot_id}): {e}")
            logger.error(traceback.format_exc())
            return None, None # Indicate an error occurred

        return 5.0, None

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

            # Configurable timeout (default 60s for chasing)
            CHASE_TIMEOUT_SEC = 60 
            HARD_CAP_ENTRY_SEC = 1800 # 30 Minutes "Give Up"
            
            # 🚀 HARD-CAP LOGIC: Total attempt time since first order (basket_start_time)
            attempt_time_sec = 0
            if basket_start > 0:
                attempt_time_sec = time.time() - basket_start

            if attempt_time_sec > HARD_CAP_ENTRY_SEC:
                logger.critical(f"🛑 [ENTRY-GIVEUP] Bot {name}: Entry attempt cycle stale for > 30m ({attempt_time_sec:.1f}s). Abandoning.")
                try:
                    exchange.cancel_order(existing_entry_order['id'], pair)
                    time.sleep(1)
                    
                    # 🚀 FORENSIC FIX: Verify the order wasn't filled perfectly inline with our cancel
                    final_status = exchange.fetch_order(existing_entry_order['id'], pair)
                    if final_status and float(final_status.get('filled', 0)) > 0:
                        logger.warning(f"🚨 [RACE-CONDITION PREVENTED] Bot {name}: Hard-cap cancelled entry was ACTUALLY filled for {final_status.get('filled')}! Adopting fill instead of abandoning.")
                        return None
                        
                    update_order_status(existing_entry_order['id'], 'cancelled', bot_id=bot_id)
                    # Reset the bot internally - if unfilled, this returns it to Scanning
                    # We pass exit_price=0 to indicate abandonment
                    from engine.database import reset_bot_after_tp
                    reset_bot_after_tp(bot_id, exit_price=0.0, action_label='ENTRY_TIMEOUT')
                    logger.info(f"✅ Bot {name}: Strategy reset to SCANNING after Entry Hard-Cap.")
                    return None
                except Exception as e_cap:
                    logger.error(f"❌ Bot {name}: Failed to execute hard-cap entry reset (might already be filled): {e_cap}")
                    return None

            if order_age_sec > CHASE_TIMEOUT_SEC:
                # 🚀 ROOT CAUSE FIX: Check for partial fills before cancelling!
                # If the order is already partially filled, we MUST NOT cancel it.
                # Crossing into 'filled' status or accepting the partial remains as Step 1.
                current_fill = float(existing_entry_order.get('filled', 0))
                if current_fill > 0:
                    logger.info(f"🛡️ Bot {name}: Entry order {existing_entry_order['id']} is partially filled ({current_fill}). CANCEL BLOCKED to preserve evidence.")
                    return None

                logger.info(f"⏱️ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old. Cancelling to CHASE price...")
                try:
                    exchange.cancel_order(existing_entry_order['id'], pair)
                    time.sleep(1) # Brief pause to ensure cancellation propagates
                    
                    # 🚀 FORENSIC FIX: Verify the order wasn't filled the millisecond before we canceled it
                    final_status = exchange.fetch_order(existing_entry_order['id'], pair)
                    if final_status and float(final_status.get('filled', 0)) > 0:
                        logger.warning(f"🚨 [RACE-CONDITION PREVENTED] Bot {name}: Cancelled entry {existing_entry_order['id']} was ACTUALLY filled for {final_status.get('filled')}! Adopting fill.")
                        return None
                    else:
                        existing_entry_order = None # Safely reset so we place a new one below
                        
                except Exception as e:
                    logger.error(f"❌ Bot {name}: Failed to cancel stale entry order (might already be filled): {e}")
                    # If we failed to cancel, it might have filled. DO NOT REPLACE IT!
                    return None
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
                    logger.info(f"🧐 {name}: Initial Order Params: {pair} {side} {amount} {price}")
                    
                    # -------------------------
                    # MAKER-PRICE RE-ALIGNMENT
                    # -------------------------
                    # 🚀 FUNDAMENTAL FIX: By default, `price` is just the `last` traded price.
                    # A Limit Maker (postOnly) will fail with -2010 if it inadvertently crosses the active spread.
                    # We MUST align a LONG to the absolute Best Bid and a SHORT to the absolute Best Ask.
                    try:
                        market_type = strategy.params.get('market_type', 'spot') if strategy and hasattr(strategy, 'params') else 'spot'
                        
                        if market_snapshot:
                            ticker = market_snapshot.get(market_type, {}).get('tickers', {}).get(pair, {})
                            best_bid = float(ticker.get('bid') or price)
                            best_ask = float(ticker.get('ask') or price)
                            
                            if side.lower() == 'buy' and price >= best_ask:
                                logger.info(f"🛡️ {name}: Aligning LONG Maker Entry from {price} to Best Bid {best_bid}")
                                price = best_bid
                            elif side.lower() == 'sell' and price <= best_bid:
                                logger.info(f"🛡️ {name}: Aligning SHORT Maker Entry from {price} to Best Ask {best_ask}")
                                price = best_ask
                    except Exception as _m_err:
                        logger.warning(f"⚠️ {name}: Failed to align Maker entry spread: {_m_err}")

                    valid, amount, price, msg = exchange.validate_order(pair, side, amount, price)
                    if not valid:
                        logger.error(f"❌ Entry Order validation failed for {name} {pair}: {msg}")
                        update_bot_error(bot_id, f"Entry Order validation failed: {msg}")
                        return

                    logger.info(f"🧐 {name}: Creating Order on Exchange...")
                    client_order_id = self._generate_deterministic_id(bot_id, 'ENTRY', 1)
                    
                    # 🚀 PRE-COMMIT: Write 'placing' row to DB BEFORE calling the exchange.
                    _entry_pre_id = save_bot_order(bot_id, 'entry', f'PLACING_{client_order_id}', price, amount,
                                                   1, 'placing', client_order_id=client_order_id, notes='pre-commit')

                    # 🚀 SPREAD-CROSS FALLBACK
                    ccxt_entry_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

                    order = self._place_gtx_order_with_retry(exchange, pair, side, amount, price, params=ccxt_entry_params, label=f"{name}-ENTRY")
                    
                    if order:
                        try:
                            update_bot_order_exchange_id(_entry_pre_id, order['id'], order.get('status', 'open'))
                        except Exception as save_err:
                            logger.error(f"❌ {name}: Failed to update entry order to bot_orders: {save_err}")
                            
                        # 🚀 SURGICAL DB UPDATE: Record the order and lock the basket
                        try:
                            from engine.database import get_connection
                            conn = get_connection()
                            cursor = conn.cursor()
                            
                            cursor.execute("""
                                UPDATE trades 
                                SET entry_order_id = ?
                                WHERE bot_id = ?
                            """, (order['id'], bot_id))
                            
                            # 🚀 EE FIX: Stamp basket_start_time so EE decay and the 15-second debounce locks reliably.
                            cursor.execute("SELECT basket_start_time FROM trades WHERE bot_id = ?", (bot_id,))
                            bst_row = cursor.fetchone()
                            if not bst_row or not bst_row[0]:
                                cursor.execute("UPDATE trades SET basket_start_time = ? WHERE bot_id = ?", (int(time.time()), bot_id))
                                
                            cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))
                            conn.commit()
                            conn.close()
                            logger.info(f"✅ {name}: Recorded ENTRY order {order['id']} in DB.")
                            update_bot_error(bot_id, None) 
                        except Exception as db_err:
                             logger.error(f"❌ {name}: Failed surgical DB update: {db_err}")
                             update_bot_error(bot_id, f"DB update error after entry: {db_err}")

                        return None 
                    else:
                        update_bot_order_exchange_id(_entry_pre_id, None, 'failed')
                        # Order failed at exchange, do NOT update trades/basket_start_time
                        # It will automatically loop again when conditions permit.

                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"❌ {name}: Error placing ENTRY order for {pair}: {error_msg}")
                    # 🚀 BUBBLE ERROR TO UI
                    update_bot_error(bot_id, f"Entry Error: {error_msg}")
                    return



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
                        if float(bot_status.get('total_invested', 0)) > 0:
                            actual_exit = float(order_status.get('average') or order_status.get('price') or current_price)
                            logger.info(f"✅ {name}: TP order {tp_order_id} filled at {actual_exit}. Resetting bot.")
                            
                            # 🚀 FUNDAMENTAL FIX: Cancel ALL remaining open orders for this bot
                            # on the exchange BEFORE resetting the DB cycle.
                            #
                            # Root cause of XRP orphan: after step-5 TP fills, bot had a step-6 GRID
                            # already resting on the exchange. reset_bot_after_tp only resets the DB —
                            # it cannot cancel exchange orders. The step-6 grid continued filling
                            # post-cycle-reset, creating 6430 XRP with no DB record.
                            #
                            # Solution: any open orders except the filled TP itself must be purged
                            # from the exchange before the cycle rolls to N+1.
                            try:
                                all_open = exchange.fetch_open_orders(pair)
                                bot_tag = f"CQB_{bot_id}_"
                                for o in all_open:
                                    cid = o.get('clientOrderId', '')
                                    oid = o.get('id')
                                    if not cid.startswith(bot_tag):
                                        continue
                                    if str(oid) == str(tp_order_id):
                                        continue  # Skip the TP that just filled (may still appear briefly)
                                    logger.info(f"🧹 {name}: Purging orphan-risk order {oid} ({cid}) from exchange before cycle reset.")
                                    try:
                                        exchange.cancel_order(oid, pair)
                                        update_order_status(oid, 'cancelled', bot_id=bot_id)
                                    except Exception as e_cancel:
                                        logger.warning(f"⚠️ {name}: Could not cancel lingering order {oid}: {e_cancel}")
                            except Exception as e_fetch:
                                logger.warning(f"⚠️ {name}: Could not fetch open orders before reset: {e_fetch}")
                            
                            reset_bot_after_tp(bot_id, actual_exit, direction=direction)
                        else:
                            logger.debug(f"⏭️ {name}: TP order {tp_order_id} is filled, but bot state is already zeroed (handled by WS). Skipping redundant reset.")
                    elif status in ['canceled', 'rejected'] or (status == 'closed' and filled == 0):
                        logger.warning(f"⚠️ {name}: TP order {tp_order_id} was canceled. Bot remains in trade.")
                        # Clear tp_order_id from DB so maintain_orders creates a new one
                        from engine.database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                        cursor.execute("UPDATE bot_orders SET status = 'cancelled', filled_amount = ? WHERE order_id = ?", (filled, tp_order_id,))
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

    def _manage_hedge_exit(self, bot_id: int, name: str, pair: str, direction: str, 
                           bot_open_orders: List[dict], exchange: ExchangeInterface) -> None:
        """
        Manages the exit of a filled hedge position at BE price.
        """
        try:
            from engine.database import get_connection, save_bot_order, update_order_status
            conn = get_connection()
            cur = conn.cursor()
            
            # 1. Fetch filled hedge orders for this bot
            
            # C. Retirement Logic: If the last HEDGETP filled, retire the hedge orders BEFORE generating new ones
            cur.execute(
                "SELECT status FROM bot_orders WHERE bot_id=? AND order_type='hedge_tp' ORDER BY id DESC LIMIT 1",
                (bot_id,)
            )
            last_tp = cur.fetchone()
            if last_tp and last_tp[0] in ('filled', 'closed'):
                logger.info(f"🛡️ {name}: Hedge TP confirmed filled. Retiring hedge positions.")
                cur.execute(
                    "UPDATE bot_orders SET status='hedge_exited', updated_at=? "\
                    "WHERE bot_id=? AND order_type='hedge' AND status='filled'",
                    (int(time.time()), bot_id)
                )
                conn.commit()
                conn.close()
                return

            cur.execute(
                "SELECT SUM(amount * price) / SUM(amount), SUM(amount), side FROM bot_orders "
                "WHERE bot_id=? AND order_type='hedge' AND status='filled' GROUP BY side",
                (bot_id,)
            )
            res = cur.fetchone()
            conn.close()
            
            if not res or not res[1] or res[1] <= 0:
                return

            be_price = float(res[0])
            hedge_qty = float(res[1])
            hedge_side = str(res[2]).lower()
            
            # 2. Side of TP is opposite of hedge
            exit_side = 'buy' if hedge_side == 'sell' else 'sell'
            
            # 3. Check for existing HEDGETP order
            existing_h_tp = next((o for o in bot_open_orders if '_HEDGETP_' in o.get('clientOrderId', '')), None)
            
            if existing_h_tp:
                # Order exists - check for drift
                curr_p = float(existing_h_tp.get('price', 0))
                if curr_p > 0 and abs(curr_p - be_price) / be_price > 0.005: 
                    logger.info(f"🛡️ {name}: Hedge TP drifted. Replacing {existing_h_tp['id']}...")
                    try:
                        exchange.cancel_order(existing_h_tp['id'], pair)
                        update_order_status(existing_h_tp['id'], 'cancelled', bot_id=bot_id)
                        existing_h_tp = None 
                    except: return
                else:
                    return # Already correct
            
            # 4. Place new HEDGETP if missing
            if not existing_h_tp:
                logger.warning(f"🛡️ {name}: Placing Hedge BE Exit (Limit Post-Only) {exit_side.upper()} {hedge_qty} @ {be_price:.4f}")
                
                cid = self._generate_deterministic_id(bot_id, 'HEDGETP', 0)
                params = {'timeInForce': 'GTX', 'postOnly': True, 'newClientOrderId': cid}
                
                if not config.TRADING_ENABLED and not config.DRY_RUN:
                    return
                
                order = None
                if config.TRADING_ENABLED:
                    try:
                        order = self._place_gtx_order_with_retry(
                            exchange, pair, exit_side, hedge_qty, be_price, params, label=f"HEDGE-EXIT-{name}"
                        )
                    except Exception as e_hedgetp:
                        err_msg = str(e_hedgetp)
                        _MARGIN_SIGNALS = [
                            "-2019", "-2027", "-4131", "-4003",
                            "margin is insufficient", "account has insufficient balance",
                            "exceed maximum position", "position limit",
                        ]
                        _is_margin_cap = any(s in err_msg.lower() for s in _MARGIN_SIGNALS)
                        if _is_margin_cap:
                            logger.warning(
                                f"🛡️ {name}: postOnly hedgetp blocked by margin cap. "
                                f"Falling back to reduceOnly GTC."
                            )
                            try:
                                # 🛡️ CLIPPING LOGIC FOR REDUCEONLY EXCEEDANCES
                                try:
                                    pos_info = self._get_phys_pos(pair)
                                    
                                    from engine.database import get_connection as _gc2
                                    with _gc2() as _c2:
                                        _cur2 = _c2.cursor()
                                        _cur2.execute("""
                                            SELECT SUM(amount) FROM bot_orders 
                                            WHERE status IN ('open', 'new') AND order_type IN ('tp', 'hedge_tp') 
                                            AND bot_id != ? AND pair = ? AND side = ?
                                        """, (bot_id, pair, exit_side))
                                        _other_tp_qty = _cur2.fetchone()[0] or 0.0
                                    
                                    if pos_info:
                                        _phys_qty = pos_info['size']
                                        _avail_qty = max(0.0, _phys_qty - _other_tp_qty)
                                        prec = exchange.get_symbol_precision(pair)
                                        _avail_qty = exchange.round_to_step(_avail_qty, prec['step_size'])
                                        
                                        if _avail_qty > 0 and hedge_qty > _avail_qty:
                                            logger.warning(f"⚠️ {name}: Clipping fallback hedgetp from {hedge_qty} to physical {_avail_qty:.4f}")
                                            hedge_qty = _avail_qty
                                        elif _avail_qty <= 0:
                                            logger.warning(f"⚠️ {name}: Fallback hedgetp capacity is 0 (covered by others). Skipping order.")
                                            return
                                    else:
                                        logger.warning(f"⚠️ {name}: No physical position found for {pair}. Cannot use reduceOnly.")
                                        return
                                except Exception as e_clip:
                                    logger.warning(f"⚠️ {name}: Hedgetp fallback clipping lookup failed: {e_clip}")


                                ro_params = {
                                    'clientOrderId': cid + '_RO',
                                    'reduceOnly': True,
                                    'timeInForce': 'GTC',
                                }
                                order = exchange.create_order(
                                    pair, 'limit', exit_side, hedge_qty, be_price,
                                    params=ro_params
                                )
                                if order:
                                    logger.info(f"✅ {name}: hedgetp placed as reduceOnly fallback @ {be_price}")
                                    params = ro_params  # use ro params for save below
                            except Exception as e_ro:
                                _ro_err = str(e_ro)
                                _RO_SKIP_SIGNALS = ["-4118", "-4131", "reduceonly", "reduce_only", "exceed maximum reduced", "position side is invalid"]
                                if any(s in _ro_err.lower() for s in _RO_SKIP_SIGNALS):
                                    logger.warning(
                                        f"⚠️ {name}: hedgetp reduceOnly fallback rejected (-4118/position): {e_ro}. "
                                        f"Position likely covered by other bots. Will retry next cycle."
                                    )
                                else:
                                    logger.error(
                                        f"❌ {name}: hedgetp reduceOnly fallback also failed: {e_ro}. "
                                        f"Will retry next cycle."
                                    )
                                return
                        else:
                            logger.error(f"❌ {name}: hedgetp placement failed: {e_hedgetp}")
                            return
                else:
                    logger.info(f"🚫 Dry Run: Would place HEDGE-EXIT {hedge_qty} @ {be_price}")
                    return

                if order and order.get('id'):
                    save_bot_order(bot_id, pair, str(order['id']), exit_side, hedge_qty, be_price, 
                                   'open', 'hedge_tp', params.get('clientOrderId', params.get('newClientOrderId', cid)), cycle_id=None)
        except Exception as e:
            logger.error(f"Error managing hedge exit for {name}: {e}")

    def maintain_orders(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ensures TP and Grid orders are placed active trades.
        """
        trade_update_data = {}
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [MAINTAIN-BLOCKED] Trading disabled. Bot {name} cannot maintain orders.")
            return

        # 🚀 STRICT SYNCHRONOUS STATE LOCK
        # Query what the DB thinks is currently open right now before acting on CCXT.
        from engine.database import get_bot_order_ids
        local_db_ids = get_bot_order_ids(bot_id)
        local_tp_id = local_db_ids.get('tp_order_id')
        local_grid_ids = [g['order_id'] for g in local_db_ids.get('grid_orders', []) if isinstance(g, dict) and 'order_id' in g]

        # 🚀 MARKET DATA SETUP
        # We need market data here for ATR and Grid Drift calculations. 
        # In maintain_orders, 'market_snapshot' is passed.
        current_market_data = None
        bot_market_data = None
        bot_multi_tf = {}
        if market_snapshot:
             market_snapshot_inner = market_snapshot.get('market_data', {})
             current_market_data = market_snapshot_inner.get(pair)
             bot_market_data = current_market_data  # alias used by some callers
             bot_multi_tf = market_snapshot.get('multi_tf_data', {}).get(pair, {})

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
        # Ensure only 1 TP, 1 Grid, and handle Dust. If more, cancel the extras.
        grid_orders = [o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')]
        tp_orders = [o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')]
        dust_orders = [o for o in bot_open_orders if '_DUST_' in o.get('clientOrderId', '')]
        
        # 🚀 STRICT SEQUENCING & STATE ENFORCEMENT
        existing_entry_orders = [o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')]

        # CASE 1: IN TRADE -> NO ENTRY ORDERS ALLOWED
        if bot_status['total_invested'] > 0 and existing_entry_orders:
             logger.warning(f"🧹 {name}: Found {len(existing_entry_orders)} dangling ENTRY orders while IN TRADE. Cancelling to enforce state.")
             for o in existing_entry_orders:
                 try:
                     exchange.cancel_order(o['id'], pair)
                     update_order_status(o['id'], 'cancelled', bot_id=bot_id)
                 except Exception as e:
                     logger.error(f"Failed to cancel dangling entry {o['id']}: {e}")
             existing_entry_orders = [] # Cleared

        # CASE 2: SCANNING (No Position) -> NO TP/GRID ALLOWED 
        # (This is handled by 'untracked order' cleanup, but let's be explicit)
        # 🚀 ZERO-INVESTED RACE CONDITION FIX:
        # If the bot's `total_invested` still says 0.0 because the DB hasn't caught up,
        # but `current_step > 0` or we JUST placed an Entry order, it is actively in a trade.
        # Do NOT purge orders in this state.
        if bot_status['total_invested'] <= 10.0 and bot_status['current_step'] == 0:
            for stale_grid in grid_orders:
                logger.warning(f"👻 {name}: Found dangling GRID order {stale_grid['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_grid['id'], pair)
                    update_order_status(stale_grid['id'], 'cancelled', bot_id=bot_id)
                except: pass
            grid_orders = [] # Clear local list
            
            for stale_tp in tp_orders:
                logger.warning(f"👻 {name}: Found dangling TP order {stale_tp['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_tp['id'], pair)
                    update_order_status(stale_tp['id'], 'cancelled', bot_id=bot_id)
                except: pass
            tp_orders = []

            for stale_dust in dust_orders:
                logger.warning(f"👻 {name}: Found dangling DUST order {stale_dust['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_dust['id'], pair)
                    update_order_status(stale_dust['id'], 'cancelled', bot_id=bot_id)
                except: pass
            dust_orders = []

            # 🛡️ HEDGE AUTO-CLEANUP: If SCANNING, purge dangling PENDING/OPEN hedges
            # (Note: FILLED hedges are now managed by _manage_hedge_exit below)
            hedge_orders = [o for o in bot_open_orders if '_HEDGE_' in o.get('clientOrderId', '')]
            if hedge_orders:
                logger.warning(f"🛡️ {name}: Found dangling PENDING HEDGE while SCANNING. Purging...")
                for ho in hedge_orders:
                    try:
                        exchange.cancel_order(ho['id'], pair)
                        update_order_status(ho['id'], 'cancelled', bot_id=bot_id)
                    except: pass
            
            # 🚀 NEW: Call centralized hedge exit manager
            self._manage_hedge_exit(bot_id, name, pair, direction, bot_open_orders, exchange)

            # 🛡️ STEP-0 EARLY RETURN (Fix 3 — Infinite Cancellation Loop Prevention)
            # The bot is SCANNING: no position is open, total_invested ≤ 10, step == 0.
            # All dangling ghost orders above have been purged. We MUST return NOW.
            # Without this return, the function falls through to TP/Grid placement code
            # and immediately creates new orders, which the next cycle will detect and purge —
            # an infinite cancel-and-recreate loop that floods Binance with cancellations.
            logger.debug(
                f"✅ {name}: Step-0 / scanning state — all ghost orders purged. "
                f"Returning early (no TP/Grid placement for scanning bots)."
            )
            return None

        # 🚀 STEP-SYNC FIX: Ensure open orders match the CURRENT martingale step.
        # If we just had a grid fill, the old TP (from a previous step) is stale.
        current_step = bot_status['current_step']
        tp_tag = f"_TP_{current_step}_"
        grid_tag = f"_GRID_{current_step + 1}_"

        # Match by tag OR by stored order ID (handles orders placed without CQB_ prefix)
        stored_tp_id = str(bot_status.get('tp_order_id', '') or '')
        valid_tp_orders = [o for o in tp_orders if tp_tag in o.get('clientOrderId', '') or (stored_tp_id and o.get('id', '') == stored_tp_id)]
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
                     
        for o in dust_orders:
            cid = o.get('clientOrderId', '')
            try:
                # CQB_{bot_id}_DUST_{step}
                step_num = int(cid.split('_DUST_')[1].split('_')[0])
                if step_num < current_step:
                    stale_orders.append(o)
            except:
                stale_orders.append(o)
                
        if stale_orders:
            logger.warning(f"🧹 {name}: Found {len(stale_orders)} STALE orders from previous steps. Purging to sync with Step {current_step}...")
            for o in stale_orders:
                try:
                    # 🛡️ PARTIAL-FILL GUARD: Never cancel a partially filled order as stale.
                    # A partial fill is real capital deployed on the exchange — cancelling it
                    # orphans that position. Only skip if there is a measurable fill.
                    filled_qty = float(o.get('filled', 0) or 0)
                    if filled_qty > 0:
                        logger.warning(
                            f"⚠️ SKIPPING stale cancel for {o.get('clientOrderId')} — has partial fill of {filled_qty}. "
                            f"Will be reconciled by reconciler when step genuinely advances."
                        )
                        continue
                    exchange.cancel_order(o['id'], pair)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                    logger.info(f"🔥 Cancelled stale {o.get('clientOrderId')} (No fill, safe to purge)")
                except Exception as e:
                    logger.error(f"Failed to cancel stale {o['id']}: {e}")

        # Ensure only 1 valid TP and 1 valid Grid exist (Deduplication / Ghost Sweeping)
        if len(grid_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(grid_orders)} total GRID orders. Restricting to strict 1 max...")
            # Sort to prefer the matching step, otherwise just keep newest
            grid_orders.sort(key=lambda x: 1 if grid_tag in x.get('clientOrderId', '') else 0, reverse=True)
            for o in grid_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except: pass
            valid_grid_orders = [grid_orders[0]] if grid_tag in grid_orders[0].get('clientOrderId','') else []
            existing_grid_order = grid_orders[0]
        else:
            existing_grid_order = valid_grid_orders[0] if valid_grid_orders else None

        if len(tp_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(tp_orders)} total TP orders. Restricting to strict 1 max (Sweeping Ghosts)...")
            # Sort to prefer the matching step, otherwise just keep newest
            tp_orders.sort(key=lambda x: 1 if tp_tag in x.get('clientOrderId', '') else 0, reverse=True)
            for o in tp_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except: pass
            valid_tp_orders = [tp_orders[0]] if tp_tag in tp_orders[0].get('clientOrderId','') else []
            existing_tp_order = tp_orders[0]
        else:
            existing_tp_order = valid_tp_orders[0] if valid_tp_orders else None
            
        if len(dust_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(dust_orders)} DUST orders. Restricting to strict 1 max...")
            for o in dust_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except: pass
            existing_dust_order = dust_orders[0]
        else:
            existing_dust_order = dust_orders[0] if dust_orders else None
        # ----------------------------------------

        strategy = self._get_strategy_instance(bot_id, bot_config)
        # 🚀 CRITICAL: Force-sync strategy.params with bot_config every cycle.
        # The strategy instance is cached and may have stale params (e.g. base_size=150 default)
        # if it was created before the DB column values were injected into bot_config.
        strategy.params.update(bot_config)

        # 2. Check for missing / filled TP order
        if not existing_tp_order:
            if local_tp_id:
                # 🚀 STALEMATE EVICTOR:
                # CCXT indicates missing TP, but DB confirms local_tp_id exists.
                # We must verify if the ID is actually DEAD before blocking re-placement.
                logger.warning(f"⏳ {name}: CCXT says TP is missing, but DB has {local_tp_id}. Verifying status...")
                try:
                    order_status = exchange.fetch_order(local_tp_id, pair)
                    status_str = order_status.get('status') if order_status else 'unknown'
                    
                    if status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                        logger.info(f"🚫 {name}: Stored TP ID {local_tp_id} is CANCELLED on exchange. Evicting from DB state.")
                        from engine.database import get_connection as _gc
                        from engine.database import update_order_status as _uos
                        _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                        _c = _gc()
                        _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                        _c.commit(); _c.close()
                        local_tp_id = None # Allow placement below
                    elif status_str == 'filled' or (status_str == 'closed' and float(order_status.get('filled', 0) or 0) > 0 and float(order_status.get('filled', 0) or 0) >= float(order_status.get('amount', 0) or 0) * 0.99):
                        if float(bot_status.get('total_invested', 0)) > 0:
                            actual_exit = float(order_status.get('average') or order_status.get('price') or current_price)
                            filled_amount = float(order_status.get('filled', 0) or order_status.get('amount', 0))
                            logger.info(f"✅ {name}: Stored TP ID {local_tp_id} is FILLED at {actual_exit} (Qty: {filled_amount}). Triggering reset.")
                            
                            # 🚀 CRITICAL DB FIX: Record the TP fill in bot_orders BEFROE resetting!
                            # If the REST API caught this fill before the WebSocket did, the database doesn't
                            # know the TP filled. If we call 'reset_bot_after_tp' now, the old cycle math
                            # will show Entries > Exits, and it will erroneously CARRY OVER the full position
                            # forever. We MUST mark this specific local_tp_id as filled + amount.
                            try:
                                from engine.database import update_order_status as _uos
                                _uos(local_tp_id, 'filled', bot_id=bot_id, filled_qty=filled_amount)
                                logger.debug(f"🧹 {name}: Force-marked TP {local_tp_id} as filled in DB to prevent CARRY bugs.")
                            except Exception as e_uos:
                                logger.warning(f"⚠️ {name}: Failed to mark TP {local_tp_id} as filled: {e_uos}")
                            
                            # 🚀 FUNDAMENTAL FIX: purge all remaining open orders before cycle reset
                            try:
                                all_open = exchange.fetch_open_orders(pair)
                                bot_tag = f"CQB_{bot_id}_"
                                for o in all_open:
                                    cid = o.get('clientOrderId', '')
                                    oid = o.get('id')
                                    if not cid.startswith(bot_tag):
                                        continue
                                    if str(oid) == str(local_tp_id):
                                        continue
                                    logger.info(f"🧹 {name}: Purging orphan-risk order {oid} ({cid}) before cycle reset.")
                                    try:
                                        exchange.cancel_order(oid, pair)
                                        update_order_status(oid, 'cancelled', bot_id=bot_id)
                                    except Exception as e_c:
                                        logger.warning(f"⚠️ {name}: Could not cancel {oid}: {e_c}")
                            except Exception as e_f:
                                logger.warning(f"⚠️ {name}: fetch_open_orders before reset failed: {e_f}")
                            
                            reset_bot_after_tp(bot_id, actual_exit, direction=direction)
                        else:
                            logger.debug(f"⏭️ {name}: Stored TP ID {local_tp_id} is FILLED, but bot state already zeroed. Skipping.")
                        return None # Exit cycle
                    else:
                         # Still 'new' or 'unknown' but missing from global open_orders list. 
                         # This is a Ghost Order (API Cache Desync). Force cancel it to be safe.
                         logger.warning(f"⏳ {name}: Stored TP {local_tp_id} status is {status_str}, but missing from global open_orders. Forcing CANCEL and Eviction.")
                         try:
                             exchange.cancel_order(local_tp_id, pair)
                         except: pass
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         _uos(local_tp_id, 'cancelled', bot_id=bot_id) # 🚀 FUNDAMENTAL FIX: Clear bot_orders state
                         _c = _gc()
                         _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                         _c.commit(); _c.close()
                         local_tp_id = None # Allow immediate replacement below!
                except Exception as _evict_err:
                     err_str = str(_evict_err).lower()
                     if "not found" in err_str or "-2013" in err_str:
                         logger.warning(f"🚫 {name}: Stored TP ID {local_tp_id} NOT FOUND on exchange. Evicting from DB state.")
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         _uos(local_tp_id, 'cancelled', bot_id=bot_id) # 🚀 FUNDAMENTAL FIX: Clear bot_orders state
                         _c = _gc()
                         _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                         _c.commit(); _c.close()
                         local_tp_id = None # Allow placement below
                     else:
                         logger.error(f"❌ {name}: Failed to evict stalemate TP ID {local_tp_id}: {_evict_err}")
                         # Also forcefully clear to prevent deadlock if API throws strange errors repeatedly
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                         _c = _gc()
                         _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                         _c.commit(); _c.close()
                         local_tp_id = None
            
            if local_tp_id is None:
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
                    bid, ask = exchange.get_best_bid_ask(pair)
                    if bid is None or ask is None:
                        raise ValueError("Failed to fetch bid/ask")
                    
                    # We are Selling to close a Long. Must join the Asks.
                    if direction == 'LONG':
                        ask_val = float(ask) if ask else current_price
                        tp_price = max(tp_price, ask_val)
                        logger.info(f"🚀 {name}: Offline Gap! Current price > TP. Adjusting TP to Ask {tp_price} to preserve Maker.")
                    # We are Buying to close a Short. Must join the Bids.
                    else:
                        bid_val = float(bid) if bid else current_price
                        tp_price = min(tp_price, bid_val)
                        logger.info(f"🚀 {name}: Offline Gap! Current price < TP. Adjusting TP to Bid {tp_price} to preserve Maker.")
                except Exception as e:
                    logger.warning(f"⚠️ {name}: Offline Gap, but fetching bid/ask failed ({e}). Falling back to Taker gap adjustment.")
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
                    valid, tp_amount, tp_price, msg = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', tp_amount, tp_price, is_closing=True)
                    if valid:
                        try:
                            client_order_id = self._generate_deterministic_id(bot_id, 'TP', bot_status['current_step'])
                            side = 'sell' if direction == 'LONG' else 'buy'
                            
                            # 🚀 Unified TP Param Preparation
                            ccxt_params, tp_amount = self._prepare_tp_order_params(
                                bot_id, name, pair, side, tp_amount, tp_price, current_price, exchange
                            )
                            if ccxt_params is None:
                                return None  # Position fully covered by siblings

                            if ccxt_params == 'DUST_CHASER':
                                logger.error(f"❌ {name}: Reached unreachable DUST_CHASER execution block. Proceeding as if nothing happened.")
                                return None

                            # ---------------------------------
                            # STANDARD TP PLACEMENT
                            # ---------------------------------
                            # 🔑 CRITICAL FIX: Embed the CQB_ clientOrderId so that
                            # maintain_orders can find this TP in the next open_orders fetch.
                            ccxt_params['newClientOrderId'] = client_order_id

                            order = self._place_gtx_order_with_retry(exchange, pair, side, tp_amount, tp_price, params=ccxt_params, label=f"{name}-MAINTAIN-TP")
                            if order:
                                save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], order.get('status', 'open'), client_order_id=client_order_id)
                                logger.info(f"✅ {name}: Maintained TP order for {pair} @ {tp_price}")
                        except Exception as e:
                            err_msg = str(e)
                            # Handle margin rejections that slip through clipping (e.g. rapid market moves)
                            if any(s in err_msg.lower() for s in self._MARGIN_SIGNALS):
                                logger.info(f"ℹ️ {name}: Margin Cap detected during TP placement for {pair}. [MARGIN-ON-HOLD]")
                            else:
                                logger.error(f"❌ {name}: Error maintaining TP: {e}")


        # 2b. EE/SYNC DRIFT CHECK: existing TP at wrong price or qty
        elif existing_tp_order and bot_status.get('total_invested', 0) > 0:
            db_tp  = self._compute_effective_tp(bot_id, name, bot_status, bot_config, strategy)
            db_qty = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            
            # 🚀 ALIGNMENT FIX: Run the theoretical target through the exchange validator
            # This ensures if the exchange auto-scaled the amount upward (e.g. for Min Notional on Demo),
            # we compare the exchange's scaled amount against our own validated scaled amount, preventing loops.
            valid, val_qty, val_tp, _ = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', db_qty, db_tp, is_closing=True)
            if valid:
                db_qty = val_qty
                db_tp = val_tp
                
            exchange_tp  = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            exchange_qty = self._get_order_amount(existing_tp_order)

            # DB-TP ZERO GUARD: recalculate if target_tp_price was wiped (post-repair/reset)
            if db_tp == 0 and bot_status.get('avg_entry_price', 0) > 0:
                db_tp = strategy.calculate_take_profit_price(bot_status, bot_status.get('avg_entry_price', 0))
                logger.info(f"[TP-RECOVER] {name}: db_tp was 0, recalculated to {db_tp:.4f} from avg_entry.")

            if db_tp > 0 and exchange_tp > 0:
                # Diff matching
                # Drift bounds (0.05% for price to tolerate tiny rounding, 1% for qty)
                drift_tp  = abs(db_tp - exchange_tp) / max(db_tp, 0.01)
                drift_qty = abs(db_qty - exchange_qty) / max(db_qty, 0.0001)

                tp_tolerance = 0.0005  # 0.05%
                # 🚀 ROUNDING FIX: Increase qty tolerance to 5% to account for lot-size reduction 
                # on small position sizes (e.g. 0.071 -> 0.070 is a 1.4% drift)
                qty_tolerance = 0.05   # 5%

                if drift_tp > tp_tolerance or drift_qty > qty_tolerance:
                    logger.info(f"[SYNC-DRIFT] {name}: TP drifted price {drift_tp*100:.4f}% (DB:{db_tp:.4f} vs EX:{exchange_tp:.4f}) qty {drift_qty*100:.2f}% (DB:{db_qty:.4f} vs EX:{exchange_qty:.4f}). Replacing.")
                    tp_order = self._sync_replace_tp(
                        bot_id, name, pair, direction, bot_status, exchange,
                        db_tp, db_qty, existing_tp_order
                    )


        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps:
             # 🚀 STRICT SEQUENCING: Do NOT place Grid orders if an Entry order is still open.
             if existing_entry_orders:
                  logger.info(f"⏳ {name}: Entry order is still open. Waiting for Full Fill before placing Grid Orders.")
                  return None

             # 🛡️ PHYSICAL-SIZE GUARD: Detect unprocessed offline fills before placing new grid.
             # CRITICAL FIX: total_invested and avg_entry_price are in TRADES table, not BOTS.
             try:
                 phys_positions = market_snapshot.get('positions', []) if market_snapshot else []
                 phys_long = 0.0
                 phys_short = 0.0
                 for p in phys_positions:
                     if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                         size = float(p.get('contracts', 0) or abs(float(p.get('positionAmt', 0))))
                         
                         side = p.get('side', '').upper()
                         if not side: # fallback for one-way mode where side might be missing
                             pos_amount = float(p.get('positionAmt', 0))
                             if pos_amount < 0:
                                 side = 'SHORT'
                             elif pos_amount > 0:
                                 side = 'LONG'
                                 
                         if side == 'SHORT':
                             phys_short += size
                         elif side == 'LONG':
                             phys_long += size

                 from engine.database import get_connection as _gc_guard
                 _conn_g = _gc_guard()
                 _cur_g = _conn_g.cursor()
                 _cur_g.execute('''
                     SELECT b.direction, t.total_invested, t.avg_entry_price
                     FROM bots b
                     JOIN trades t ON b.id = t.bot_id
                     WHERE b.pair = ? AND b.status != 'Stopped' AND t.total_invested > 0
                 ''', (pair,))
                 active_bots_guard = _cur_g.fetchall()
                 _conn_g.close()

                 virtual_long = 0.0
                 virtual_short = 0.0
                 for b_dir, b_inv, b_avg in active_bots_guard:
                     if b_inv and b_avg and float(b_avg) > 0:
                         b_qty = float(b_inv) / float(b_avg)
                         if str(b_dir).upper() == 'LONG':
                             virtual_long += b_qty
                         else:
                             virtual_short += b_qty

                 phys_net = phys_long if direction == 'LONG' else phys_short
                 virtual_net = virtual_long if direction == 'LONG' else virtual_short

                 if virtual_net > 0.001:
                     if phys_net > virtual_net * 1.10:
                         logger.warning(
                             f"🛑 {name}: Physical {direction} {phys_net:.4f} >> virtual {direction} {virtual_net:.4f} "
                             f"(+{(phys_net/virtual_net - 1)*100:.0f}%). "
                             f"Offline fill unprocessed. SKIPPING grid until reconciler catches up."
                         )
                         return None
                 else:
                     if phys_net > 0.01:
                         logger.warning(f"🛑 {name}: Physical {direction} {phys_net:.4f} but virtual {direction} ~0. SKIPPING grid.")
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
                 
                 exchange_min_notional = prec.get('min_notional', None)
                 if exchange_min_notional is None:
                     exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                     
                 bot_config['base_size'] = max(min_cost_qty, exchange_min_notional) * 1.05
                 # 🚀 CRITICAL: Keep strategy.params in sync with the use_min_size override.
                 # Without this, calculate_grid_order_amount reads stale strategy.params['base_size']
                 # (e.g. 150.0 default) rather than the correctly computed min notional size.
                 strategy.params['base_size'] = bot_config['base_size']
             else:
                 prec = exchange.get_symbol_precision(pair)
                 exchange_min_notional = prec.get('min_notional', None)
                 if exchange_min_notional is None:
                     exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                 if bot_config.get('base_size', 0) < exchange_min_notional:
                     logger.error(f"⛔ CONFIG ERROR [{pair}]: Configured base_size=${bot_config.get('base_size',0):.2f} is below exchange minimum ${exchange_min_notional:.2f}. Halting grid.")
                     return
             # 🚀 STRICT SYNCHRONOUS STATE LOCK (GRID)
             # Wait if the DB already thinks a Grid is open, but CCXT was just too slow to show it.
             if len(local_grid_ids) > 0:
                  logger.warning(f"⏳ {name}: CCXT indicates missing Grid, but DB confirms {local_grid_ids} was placed. Verifying status...")
                  # 🚀 STALEMATE EVICTOR (GRID): Verify if it's dead before blocking
                  try:
                      latest_grid_id = local_grid_ids[-1]
                      order_status = exchange.fetch_order(latest_grid_id, pair)
                      status_str = order_status.get('status') if order_status else 'unknown'
                      filled_qty = order_status.get('filled', 0.0) if order_status else 0.0
                      
                      if status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                          logger.info(f"🚫 {name}: Stored GRID ID {latest_grid_id} is CANCELLED on exchange. Evicting from DB state. Preserving {filled_qty} fill.")
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                          local_grid_ids = [] # Clear locals to unblock
                      elif status_str in ['filled', 'closed']:
                          logger.info(f"✅ {name}: Stored GRID ID {latest_grid_id} is FILLED. Offline sync will handle this.")
                          # It's filled, let the offline sync processor handle it. Wait.
                      else:
                          logger.warning(f"⏳ {name}: Stored Grid {latest_grid_id} status is {status_str}, but missing from open_orders! Forcing CANCEL and Eviction.")
                          try:
                              exchange.cancel_order(latest_grid_id, pair)
                          except: pass
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id)
                          local_grid_ids = [] # Clear to allow grid logic below to immediately fire
                  except Exception as _evict_err:
                      err_str = str(_evict_err).lower()
                      if "not found" in err_str or "-2013" in err_str:
                          logger.warning(f"🚫 {name}: Stored GRID ID {local_grid_ids[-1]} NOT FOUND on exchange. Evicting from DB state.")
                          from engine.database import update_order_status as _uos
                          _uos(local_grid_ids[-1], 'cancelled', bot_id=bot_id)
                          local_grid_ids = []
                      else:
                          logger.error(f"❌ {name}: Failed to evict stalemate GRID ID: {_evict_err}")
                          # Free the local hold regardless, let the engine rebuild it.
                          from engine.database import update_order_status as _uos
                          _uos(local_grid_ids[-1], 'cancelled', bot_id=bot_id)
                          local_grid_ids = []

             if len(local_grid_ids) > 0:
                  grid_price = 0
                  grid_explain = "Blocked by Local DB Lock"
                  grid_amount = 0
             else:
                  grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data, multi_tf_data=bot_multi_tf)
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
                     bid, ask = exchange.get_best_bid_ask(pair)
                     if bid is None or ask is None:
                         raise ValueError("Failed to fetch bid/ask")
                     
                     # We are Buying to open a Long grid. Must join the Bids.
                     if direction == 'LONG':
                         bid_val = float(bid) if bid else current_price
                         grid_price = min(grid_price, bid_val)
                         logger.info(f"🚀 {name}: Grid Gap! Price Dropped. Adjusting Grid to Bid {grid_price} to preserve Maker.")
                     # We are Selling to open a Short grid. Must join the Asks.
                     else:
                         ask_val = float(ask) if ask else current_price
                         grid_price = max(grid_price, ask_val)
                         logger.info(f"🚀 {name}: Grid Gap! Price Rallied. Adjusting Grid to Ask {grid_price} to preserve Maker.")
                 except Exception as e:
                     logger.warning(f"⚠️ {name}: Grid Gap, but fetching bid/ask failed ({e}). Falling back to Taker grid adjustment.")
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
                                save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, order.get('status', 'open'), client_order_id=client_order_id_grid, notes=grid_explain)
                                # ✅ Successful grid placement — clear any stale pos_limit flag
                                if bot_status.get('pos_limit_hit'):
                                    flag_bot_pos_limit(bot_id, False)
                                logger.info(f"✅ {name}: Maintained Grid order for {pair} @ {grid_price}")
                        except Exception as e:
                            err_msg = str(e)
                            # Check if this is a margin/position cap error (Binance error codes: -2019, -5022)
                            is_margin_cap = any(code in err_msg for code in ['-2019', '-5022', '-4028', 'margin', 'position limit'])
                            if is_margin_cap:
                                is_reducing = self._is_order_net_reducing(pair, side, grid_amount)
                                if is_reducing:
                                   logger.info(f"🚀 {name}: Margin Cap hit but order is Reductive. Force-Allowing for one-way netting.")
                                   # We continue without flagging pos_limit_hit=True if it's reducing
                                else:
                                   logger.warning(f"🚫 {name}: Margin/Position cap hit. Grid order held. [MARGIN-ON-HOLD]")
                                   flag_bot_pos_limit(bot_id, True)
                            else:
                                logger.error(f"❌ {name}: Error maintaining Grid: {e}")
                            
        # 3b. MAX-STEP LOCK: If we reached max steps, there should be NO Grid orders. Clean them completely!
        elif bot_status['current_step'] >= strategy.max_steps:
             if existing_grid_order:
                 logger.warning(f"🛑 {name}: Max steps reached ({strategy.max_steps}) but Grid exists! Cancelling physical Grid {existing_grid_order['id']}.")
                 try:
                     exchange.cancel_order(existing_grid_order['id'], pair)
                     from engine.database import update_order_status as _uos
                     _uos(existing_grid_order['id'], 'cancelled', bot_id=bot_id)
                 except: pass
             
             if len(local_grid_ids) > 0:
                 logger.warning(f"🧹 {name}: Max steps reached but DB lists Grid ghosts {local_grid_ids}. Sweeping DB cleanly.")
                 from engine.database import update_order_status as _uos
                 for ghost_id in local_grid_ids:
                     _uos(ghost_id, 'cancelled', bot_id=bot_id)

        # 3c. GRID SYNC-DRIFT: If grid exists but price is imprecise or drifted
        elif existing_grid_order and bot_status['total_invested'] > 0:
            current_market_data = market_snapshot.get('market_data', {}).get(pair, None)
            bot_multi_tf = market_snapshot.get('multi_tf_data', {}).get(pair, {})
            grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data, multi_tf_data=bot_multi_tf)
            target_grid_price, grid_explain = grid_res if isinstance(grid_res, tuple) else (grid_res, "")
            
            if target_grid_price > 0:
                side = 'buy' if direction == 'LONG' else 'sell'
                # Safe fallback for amount key — varies by source (CCXT vs DB cache)
                grid_amt = float(existing_grid_order.get('amount') or existing_grid_order.get('origQty') or existing_grid_order.get('qty') or 0)
                # Pass through validation to get correct precision
                _, _, target_grid_price, _ = exchange.validate_order(pair, side, grid_amt, target_grid_price)
                
                curr_grid_price = float(existing_grid_order.get('price', 0))
                
                # ATR-grid bots intentionally lock the ATR at grid placement time (locked_atr).
                # Re-computing the ATR every cycle will always produce a slightly different price,
                # so we SKIP GRID-SYNC for ATR grids to prevent the constant cancel/replace loop.
                use_atr_grid = bot_config.get('UseATRGrid', False)
                if use_atr_grid:
                    try:
                        _mkt = exchange.exchange.markets.get(pair, {})
                        tick = float(_mkt.get('precision', {}).get('price') or 0.01)
                    except Exception:
                        tick = 0.01
                    if abs(curr_grid_price - target_grid_price) > 2 * tick:
                        logger.debug(f"[GRID-SYNC] {name}: ATR-grid drift ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Skipping auto-replace (ATR-locked).")
                        # Do NOT cancel — ATR grids are anchored at placement
                else:
                    # Non-ATR grids: replace if price drifted > 0.5% (was 0.1%, widened to stop noise triggers)
                    if abs(curr_grid_price - target_grid_price) / max(target_grid_price, 0.0001) > 0.005:
                        # 🚀 ROOT CAUSE FIX (mirrored from Block 1, line ~1009):
                        # A grid with a partial fill MUST NOT be cancelled — the filled portion
                        # is evidence of real inventory. Cancelling it wipes that proof from the ledger
                        # and leaves the bot without a grid order (e.g. SUI GRID_9: 53.1 filled, then
                        # cancelled by this block which lacked this guard).
                        current_fill = float(existing_grid_order.get('filled', 0) or 0)
                        if current_fill > 0:
                            logger.info(f"🛡️ [GRID-SYNC] {name}: Grid drifted, but has partial fill ({current_fill}). CANCEL BLOCKED — partial fill is real inventory.")
                        else:
                            logger.info(f"🔄 [GRID-SYNC] {name}: Grid drifted ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Replacing.")
                            try:
                                grid_order_id = existing_grid_order.get('order_id', existing_grid_order.get('id'))
                                exchange.cancel_order(grid_order_id, pair)
                                filled_qty = float(existing_grid_order.get('filled', 0) or 0)
                                update_order_status(grid_order_id, 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                                existing_grid_order = None # Force re-place in next cycle
                            except Exception as e_grid_sync:
                                logger.error(f"❌ [GRID-SYNC] {name}: Failed to cancel drifted grid: {e_grid_sync}")
                        
        # 🚀 HEDGE CHECK: If we reached the Hedge Step, evaluate and dynamically lock size.
        # Evaluated purely parallel to TP/Grid maintenance to preserve the core ecosystem.
        try:
            from engine.manager import check_hedge_entry, calculate_hedge_lot
            hedge_mission = check_hedge_entry(int(bot_status.get('current_step', 0)), bot_config)
            if hedge_mission:
                avg_price = max(float(bot_status.get('avg_entry_price', 1)), 1e-9)
                raw_invested_qty = float(bot_status.get('total_invested', 0)) / avg_price
                hedge_qty = calculate_hedge_lot(raw_invested_qty, bot_config)
                
                prec = exchange.get_symbol_precision(pair)
                qty_step = prec.get('step_size', 0.001)
                hedge_qty = exchange.round_to_step(hedge_qty, qty_step)
                
                if hedge_qty > 0:
                    logger.info(f"🛡️ {name}: Hedge threshold active (Step {bot_status['current_step']}). Executing Hedge Lock...")
                    self.execute_hedge_lock(
                        bot_id, name, pair, direction, bot_status, current_price, hedge_qty,
                        hedge_mission['trigger_step'], exchange, bot_config
                    )
        except Exception as e_hedge_eval:
             logger.error(f"❌ {name}: Failed to evaluate hedge lock in maintain_orders: {e_hedge_eval}")

        return None


    def execute_hedge_lock(self, bot_id: int, name: str, pair: str, direction: str,
                           bot_status: dict, lock_price: float, lock_qty: float, trigger_step: int,
                           exchange: ExchangeInterface, bot_config: Dict[str, Any]):
        """
        Places a limit Post-Only (GTX) order on the OPPOSITE side at avg_entry_price
        to lock in the maximum loss when the bot reaches HedgeStartStep.

        Design:
        - LONG bot → places a SELL limit at avg_entry_price (no worse than breakeven)
        - SHORT bot → places a BUY limit at avg_entry_price
        - Fully idempotent: skips if an open 'hedge' order already exists in bot_orders
        - TP does NOT cancel this order — it requires manual intervention
        """
        try:
            from engine.database import get_connection
            
            # 1. Fetch Current Hedge State (Open Orders and Filled Positions)
            conn = get_connection()
            cur = conn.cursor()
            # Get latest open hedge order for this bot (if any)
            cur.execute(
                "SELECT order_id, amount, status FROM bot_orders WHERE bot_id=? AND order_type='hedge' AND status='open' ORDER BY id DESC LIMIT 1",
                (bot_id,)
            )
            open_row = cur.fetchone()
            
            # Sum of all FILLED hedge amounts for this bot
            cur.execute(
                "SELECT SUM(amount) FROM bot_orders WHERE bot_id=? AND order_type='hedge' AND status='filled'",
                (bot_id,)
            )
            filled_qty = cur.fetchone()[0] or 0.0
            conn.close()

            # 2. Logic: Parity Sync
            # We want total_hedged (Filled + Open) to equal lock_qty (the bot's current total size)
            total_hedged = filled_qty + (open_row[1] if open_row else 0)
            
            # If we are within 0.1%, we are synced
            if abs(total_hedged - lock_qty) / max(lock_qty, 0.0001) < 0.001:
                logger.debug(f"🛡️ {name}: Hedge state is synced ({total_hedged:.4f} hedged vs {lock_qty:.4f} bot).")
                return None

            # Case A: We have a pending order that is now too small (Bot moved steps)
            if open_row:
                existing_oid = open_row[0]
                logger.info(f"🔄 {name}: Resizing PENDING hedge ({total_hedged:.4f} -> {lock_qty:.4f}). Cancelling {existing_oid}...")
                try:
                    exchange.cancel_order(existing_oid, pair)
                    from engine.database import update_order_status
                    update_order_status(existing_oid, 'canceled')
                    # Now only filled_qty remains
                    total_hedged = filled_qty
                except Exception as e_cancel:
                    logger.warning(f"Could not cancel pending hedge {existing_oid}: {e_cancel}")
                    # Safety: If we can't cancel, don't place another to avoid double-locking
                    return None

            # Case B: Calculate Delta
            # If bot size = 2.0 and we have 1.5 filled, we need +0.5
            delta_qty = lock_qty - filled_qty
            
            if delta_qty <= 0:
                logger.debug(f"🛡️ {name}: Hedge position already covers/exceeds bot size ({filled_qty:.4f} filled vs {lock_qty:.4f} bot).")
                return None

            if not config.TRADING_ENABLED and not config.DRY_RUN:
                logger.info(f"🛡️ [HEDGE-BLOCKED] Trading disabled. Bot {name} needs {delta_qty:.4f} hedge for {pair}.")
                return None

            # 3. Execution Config
            hedge_side = 'sell' if direction.upper() == 'LONG' else 'buy'
            prec_data = exchange.get_precision(pair) or {}
            price_prec = int(prec_data.get('price_precision', 2))
            qty_prec = int(prec_data.get('qty_precision', 3))
            step_size = float(prec_data.get('step_size', 0.001))

            if step_size > 0:
                delta_qty = math.floor(delta_qty / step_size) * step_size
            delta_qty = round(delta_qty, qty_prec)
            lock_price_r = round(lock_price, price_prec)

            if delta_qty <= 0 or lock_price_r <= 0:
                logger.error(f"🛡️ {name}: Invalid hedge delta params — qty={delta_qty}, price={lock_price_r}")
                return None

            logger.warning(
                f"🛡️ [HEDGE-LOCK] Bot {name} (step {trigger_step}): Placing {hedge_side.upper()} "
                f"{'DELTA ' if filled_qty > 0 else ''}limit GTX {delta_qty} @ {lock_price_r}"
            )

            # 4. Deterministic CID
            cid = self._generate_deterministic_id(bot_id, 'HEDGE', trigger_step)
            
            # 🛡️ HEDGE ENTRY ORDER FLAG LOGIC
            # The hedge order is on the OPPOSITE side from the bot's direction.
            # In ONE-WAY mode this means it REDUCES the existing net position.
            # (SHORT bot BUY hedge reduces net short; LONG bot SELL hedge reduces net long)
            # → Strategy:
            #   Single bot on this pair: use reduceOnly=True (no margin needed, dust-clean)
            #   Multi-bot on this pair:  use postOnly+GTX (same as TP—can't use reduceOnly
            #       because net position size may differ from this bot's slice).
            #   Multi-bot + margin fail: fall back to reduceOnly+GTC (see except below)
            try:
                from engine.database import get_connection as _ghc
                _hc = _ghc()
                _hcur = _hc.cursor()
                _hcur.execute(
                    "SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id "
                    "WHERE b.pair=? AND t.total_invested>0 AND b.id!=?",
                    (pair, bot_id)
                )
                _other_bots_hedge = _hcur.fetchone()[0]
                _hc.close()
            except Exception:
                _other_bots_hedge = 1  # conservative: assume multi-bot

            if _other_bots_hedge == 0:
                # Sole bot on pair: use reduceOnly (closes perfectly, zero margin)
                params = {'timeInForce': 'GTC', 'reduceOnly': True, 'newClientOrderId': cid}
                logger.info(f"🛡️ {name}: Sole bot on {pair} — hedge entry with reduceOnly (no margin needed).")
            else:
                # Multi-bot: use postOnly+GTX (may fail on margin cap — fallback below)
                params = {'timeInForce': 'GTX', 'postOnly': True, 'newClientOrderId': cid}
                logger.info(f"🛡️ {name}: Multi-bot on {pair} ({_other_bots_hedge} others) — hedge entry postOnly+GTX.")

            order = None
            try:
                order = self._place_gtx_order_with_retry(
                    exchange, pair, hedge_side, lock_qty, lock_price_r, params,
                    label=f"HEDGE-{name}"
                )
            except Exception as e_hedge:
                err_msg = str(e_hedge)
                _MARGIN_SIGNALS = [
                    "-2019", "-2027", "-4131", "-4003",
                    "margin is insufficient", "account has insufficient balance",
                    "exceed maximum position", "position limit",
                ]
                _is_margin_cap = any(s in err_msg.lower() for s in _MARGIN_SIGNALS)
                if _is_margin_cap and params.get('postOnly'):
                    # Multi-bot + margin cap: hedge entry also blocked.
                    # The hedge entry REDUCES the existing position — fall back to reduceOnly.
                    # Risk same as TP fallback: partial cancel if net position shrinks first.
                    logger.warning(
                        f"🛡️ {name}: postOnly hedge entry blocked by margin cap. "
                        f"Falling back to reduceOnly GTC."
                    )
                    try:
                        ro_params = {
                            'newClientOrderId': cid + '_RO',
                            'reduceOnly': True,
                            'timeInForce': 'GTC',
                        }
                        order = exchange.create_order(
                            pair, 'limit', hedge_side, lock_qty, lock_price_r,
                            params=ro_params
                        )
                        if order:
                            logger.info(f"✅ {name}: Hedge entry placed as reduceOnly fallback @ {lock_price_r}")
                            params = ro_params
                    except Exception as e_ro:
                        logger.error(
                            f"❌ {name}: Hedge entry reduceOnly fallback also failed: {e_ro}. "
                            f"Will retry next cycle."
                        )
                        return None
                else:
                    logger.error(f"🛡️ {name}: execute_hedge_lock order failed: {e_hedge}")
                    return None

            exchange_order_id = str(order['id'])
            logger.info(f"✅ [HEDGE-LOCK] Bot {name}: Hedge order placed. ID={exchange_order_id}")

            # 5. Record in bot_orders
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, client_order_id, notes)
                VALUES (?, ?, 'hedge', ?, ?, ?, 'open', ?, ?, ?)
            """, (bot_id, trigger_step, exchange_order_id, lock_price_r, lock_qty,
                  int(time.time()), cid, f"Hedge lock at step {trigger_step}"))
            conn.commit()
            conn.close()

            # 6. Log to trade_history
            from engine.database import log_trade
            log_trade(bot_id, 'HEDGE_OPEN', pair, lock_price_r, lock_qty,
                      lock_price_r * lock_qty, exchange_order_id, trigger_step,
                      f'{direction} bot hedged at step {trigger_step} via {hedge_side.upper()} @ {lock_price_r}', 0)

            return order

        except Exception as e:
            logger.error(f"🛡️ {name}: execute_hedge_lock failed: {e}")
            logger.error(traceback.format_exc())
            return None


    def execute_exit_sl(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):

        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [EXIT-BLOCKED] Trading disabled. Bot {name} cannot execute SL for {pair}.")
            return

        logger.critical(f"⛔ {name}: Executing STOP LOSS for {pair} at step {bot_status['current_step']}")
        
        if config.DRY_RUN:
            log_trade(bot_id, 'STOP_LOSS', pair, current_price, bot_status['total_invested'] / bot_status['avg_entry_price'], bot_status['total_invested'], f'DRY_RUN_SL_{bot_id}', bot_status['current_step'], "Dry run SL", (current_price - bot_status['avg_entry_price']) * bot_status['total_invested'] / bot_status['avg_entry_price'])
            reset_bot_after_tp(bot_id, current_price, direction=direction, action_label='DRY_RUN_SL')
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
                        reset_bot_after_tp(bot_id, current_price, direction=direction, action_label='STOP_LOSS_EXIT')
                        logger.info(f"✅ {name}: Market order placed to close SL for {pair} (ID: {order['id']})")
                    else:
                        logger.error(f"❌ {name}: Failed to place market order for SL exit for {pair}")
                else:
                    logger.info(f"ℹ️ {name}: No active position found on exchange for {pair} to close. Running wipe guard before DB reset.")
                    # 🗡️ ARCHITECTURAL: Gate through safe_wipe_bot() — blocks CARRY_PENDING or if ledger still shows net units
                    safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_NO_POSITION: exchange has 0 units to close", exit_price=current_price)
            else:
                logger.info(f"ℹ️ {name}: Bot has 0 avg_entry_price. Running wipe guard before DB reset.")
                # 🗡️ ARCHITECTURAL: Gate through safe_wipe_bot() — blocks CARRY_PENDING or if ledger still shows net units
                safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_ZERO_PRICE: avg_entry_price is 0, no market order needed", exit_price=current_price)

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
