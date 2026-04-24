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

# API Error Tracker (Hammer Shield)
# Tracks consecutive errors per bot to detect API loops and banish them before a Binance Global Ban.
_API_ERROR_TRACKER = {}

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

    def _generate_deterministic_id(self, bot_id: int, type_str: str, cycle_id: int, step_index: int) -> str:
        """
        Generates an idempotent deterministic clientOrderId for orders.
        Format: CQB_{bot_id}_{TYPE}_{CYCLE}_{STEP}
        Adding cycle_id ensures that retries or race conditions for the same step 
        cannot place duplicate orders (Binance will reject duplicate clientOrderId).
        """
        return f"CQB_{bot_id}_{type_str.upper()}_{cycle_id}_{step_index}"

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



    def _prepare_tp_order_params(self, bot_id: int, name: str, pair: str, side: str, amount: float, tp_price: float, current_price: float, exchange: Any, direction: str) -> Tuple[Optional[Dict], float]:
        """
        Calculates Take Profit parameters using this bot's own virtual position size.
        
        Architecture (Correct):
        1. Each bot manages its OWN position. TP qty = bot's own virtual open qty from its ledger.
        2. Physical position is used as a sanity cap (can't close more than physically exists).
        3. Single-Bot Active (Dust): conditionally applies reduceOnly=True if < $5 to mathematically bypass min notional.
        4. Multi-Bot Active: drops reduceOnly, uses postOnly+GTX. If < $5, triggers DUST_CHASER abort.
        """
        # Determine active sibling bots for conditional reduceOnly capability
        try:
            from engine.database import get_connection as _ghc
            with _ghc() as _hc:
                _hcur = _hc.cursor()
                _hcur.execute(
                    "SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id "
                    "WHERE b.pair=? AND t.total_invested>0 AND b.id!=?",
                    (pair, bot_id)
                )
                _other_bots_count = _hcur.fetchone()[0]
        except Exception:
            _other_bots_count = 1  # Base assumption: multi-bot if DB fails

        # Standard baseline for all bot configurations (protects Maker rebates via PostOnly)
        ccxt_params = {'postOnly': True, 'timeInForce': 'GTX'}

        # 1. READ open_qty ACCUMULATOR — authoritative position size [v2.1]
        # trades.open_qty is maintained atomically by credit_fill() on every fill.
        # It is the exact qty confirmed by the exchange — no float-sum recomputation.
        try:
            from engine.database import get_connection as _gc_tp
            from engine.exchange_interface import normalize_symbol
            norm_pair = normalize_symbol(pair)
            with _gc_tp() as _c_tp:
                _cur = _c_tp.cursor()

                # Primary: read the accumulator directly
                _cur.execute(
                    "SELECT open_qty, cycle_id FROM trades WHERE bot_id = ?", (bot_id,)
                )
                acc_row = _cur.fetchone()
                bot_virtual_open_qty = float(acc_row[0] or 0.0) if acc_row else 0.0
                cycle_id = int(acc_row[1] or 1) if acc_row else 1

                # Fallback: if accumulator is zero but DB has fills, recompute (handles
                # bots running before v2.1 migration where open_qty was not yet populated)
                if bot_virtual_open_qty <= 0:
                    _cur.execute("""
                        SELECT
                            COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END), 0),
                            COALESCE(SUM(CASE WHEN order_type IN ('tp','close','adoption_reduce','dust_close','sl') THEN filled_amount ELSE 0 END), 0)
                        FROM bot_orders
                        WHERE bot_id=? AND (cycle_id=? OR cycle_id IS NULL)
                        AND status NOT IN ('reset_cleared','auto_closed','failed','placing')
                        AND filled_amount > 0
                    """, (bot_id, cycle_id))
                    _leg = _cur.fetchone()
                    _recomputed = max(0.0, float(_leg[0] or 0) - float(_leg[1] or 0))
                    if _recomputed > 0:
                        logger.debug(f"[TP-QTY] {name}: accumulator=0, recomputed={_recomputed:.8f} — using recomputed (pre-v2.1 bot)")
                        bot_virtual_open_qty = _recomputed
                        # Backfill the accumulator so next cycle uses it correctly
                        _c_tp.execute(
                            "UPDATE trades SET open_qty=? WHERE bot_id=?",
                            (_recomputed, bot_id)
                        )

                # UBE SANITY CAP — verify we're not trying to close more than physically exists.
                # ⚠️ ROOT CAUSE FIX (v2.3.1): The cap previously fired on ANY excess, including
                # sub-step rounding diffs from stale active_positions snapshots. This caused TP
                # qty to silently deflate (0.016 → 0.015 → 0.014), which the reconciler then
                # "corrected" with adoption_reduce fills, permanently corrupting open_qty.
                # FIX: Only cap if virtual qty exceeds physical by more than 20% (genuine corruption).
                # Sub-20% differences are explained by snapshot lag, rounding, or pending order fills.
                _cur.execute("SELECT size, side FROM active_positions WHERE pair=?", (norm_pair,))
                phys_rows = _cur.fetchall()
                bot_dir = direction.upper()
                phys_matching = sum(float(r[0]) for r in phys_rows if str(r[1]).upper() == bot_dir)
                phys_opposite = sum(float(r[0]) for r in phys_rows if str(r[1]).upper() != bot_dir)

                # Neighbor bots (for UBE context only)
                _cur.execute("""
                    SELECT b.direction, t.open_qty
                    FROM bots b JOIN trades t ON b.id=t.bot_id
                    WHERE b.normalized_pair=? AND t.open_qty>0 AND b.id!=?
                """, (norm_pair, bot_id))
                neighbors = _cur.fetchall()
                opposite_virtual_qty = sum(q for d, q in neighbors if d.upper() != bot_dir)

                if phys_matching > 0:
                    max_possible_qty = phys_matching + opposite_virtual_qty
                elif bot_virtual_open_qty > 0 and phys_matching == 0 and phys_opposite == 0:
                    max_possible_qty = bot_virtual_open_qty + opposite_virtual_qty
                    logger.debug(f"🔍 {name}: UBE: No physical row for {bot_dir} {norm_pair}. Using accumulator anchor ({bot_virtual_open_qty:.6f}).")
                else:
                    max_possible_qty = max(0.0, opposite_virtual_qty - phys_opposite)

                # Only cap if virtual is genuinely more than 20% above physical capacity.
                # This filters out snapshot lag (e.g. phys=0.015 vs virtual=0.016 due to stale row).
                ube_excess = bot_virtual_open_qty - max_possible_qty
                ube_threshold = max(0.20 * max_possible_qty, 0.0001) if max_possible_qty > 0 else 0.0001
                if ube_excess > ube_threshold:
                    logger.warning(
                        f"🛡️ {name}: UBE cap! accumulator={bot_virtual_open_qty:.6f} "
                        f"capped at {max_possible_qty:.6f} (phys={phys_matching:.6f}, "
                        f"excess={ube_excess:.6f} > threshold={ube_threshold:.6f}). "
                        f"Possible DB corruption — investigate active_positions."
                    )
                    bot_virtual_open_qty = max_possible_qty
                elif ube_excess > 0:
                    logger.debug(
                        f"🔍 {name}: UBE sub-threshold excess={ube_excess:.6f} (phys={phys_matching:.6f} vs virtual={bot_virtual_open_qty:.6f}). "
                        f"Within snapshot-lag tolerance — NOT capping. Trusting accumulator."
                    )

        except Exception as e:
            logger.warning(f"⚠️ {name}: Failed to read open_qty accumulator: {e}. Falling back to passed amount.")
            bot_virtual_open_qty = amount
            phys_matching = 0.0
            opposite_virtual_qty = 0.0

        if bot_virtual_open_qty <= 0.0:
            logger.warning(f"⚠️ {name}: open_qty is 0 — no position to close.")
            return None, None

        # 2. Derive TP qty from accumulator (already exchange-confirmed, rounding is final cleanup)
        prec = exchange.get_symbol_precision(pair)
        tp_qty = exchange.round_to_step(bot_virtual_open_qty, prec['step_size'])

        if tp_qty <= 0:
            logger.info(f"INFO {name}: open_qty rounds to 0 after step_size. Snapping accumulator to 0.")
            try:
                from engine.database import get_connection as _gc_snap
                with _gc_snap() as _sc:
                    _sc.execute("UPDATE trades SET open_qty=0 WHERE bot_id=?", (bot_id,))
            except Exception:
                pass
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
            logger.warning(f"DUST {name}: Virtual TP notional ${notional:.2f} < min ${_min_notional:.2f} (qty={tp_qty}).")
            
            if _other_bots_count == 0:
                # Sole bot fallback: Convert to a reduceOnly order which mathematically bypasses the min-notional gate.
                ccxt_params = {'timeInForce': 'GTC', 'reduceOnly': True}
                logger.info(f"✨ {name}: Sub-$5 limit detected. Sole-bot status confirmed. Switching to natively bypassed reduceOnly TP.")
            else:
                # Multi-bot setups cannot use reduceOnly, meaning Binance will strictly reject the sub-$5 limit order.
                # Since we forbid synthetic price-nudging (Strict Proof-Only), this dust cannot be closed profitably.
                logger.warning(f"🛑 {name}: Multi-bot configuration blocks reduceOnly. Cannot limit-close sub-${_min_notional:.2f} legitimately. Yielding to DUST_CHASER.")
                return 'DUST_CHASER', tp_qty

        # 6. Spread-Cross Fix: TP at market price must be GTC taker
        try:
            if tp_price == exchange.round_to_step(current_price, prec['tick_size']):
                logger.warning(f"WARN {name}: price already at or beyond TP, switching to GTC.")
                ccxt_params.pop('postOnly', None)
                ccxt_params['timeInForce'] = 'GTC'
        except Exception:
            pass

        logger.debug(f"OK {name}: TP qty={tp_qty:.4f} (virtual={bot_virtual_open_qty:.4f}) notional=${notional:.2f}")
        return ccxt_params, tp_qty


    def _place_gtx_order_with_retry(self, exchange, pair: str, side: str, amount: float, price: float, params: dict, label: str = "order", position_side: str = None) -> dict:
        """
        Places a GTX (Post-Only) limit order with automatic maker-price retry.

        Binance rejects Post-Only orders with two error codes:
          -50004: Order price would be taker (Demo FAPI)
          -2010:  Order would immediately execute (Live/Testnet FAPI)
        On either rejection, we re-fetch the live bid/ask and retry ONCE at a
        safe maker price. If the retry also fails, we drop GTX and place a plain
        limit (taker) as the ultimate fallback — ensuring the order is never silently lost.
        """
        # Inject positionSide so Binance knows which net side to affect
        if position_side and params is not None:
            params['positionSide'] = position_side.upper()

        def _is_postonly_rejected(err_str: str) -> bool:
            return (
                '-50004' in err_str or
                '-2010' in err_str or
                'Post Only' in err_str or
                'post only' in err_str.lower() or
                'would be executed immediately' in err_str.lower() or
                'would immediately' in err_str.lower()
            )

        try:
            return exchange.create_order(pair, 'limit', side, amount, price, params=params)
        except Exception as e:
            err_str = str(e)
            if not _is_postonly_rejected(err_str):
                raise  # Not a post-only issue — propagate

            logger.warning(
                f"[GTX-RETRY] {label}: Post-Only rejected ({err_str[:80]}) for "
                f"{pair} {side} @ {price:.6f}. Re-fetching bid/ask..."
            )
            try:
                bid, ask = exchange.get_best_bid_ask(pair)
            except Exception:
                bid, ask = None, None

            if bid is None or ask is None:
                logger.error(f"[GTX-RETRY] {label}: Cannot fetch bid/ask. Raising original error.")
                raise

            prec = exchange.get_symbol_precision(pair)
            tick = prec.get('tick_size', 0.0001)
            if side.lower() == 'buy':
                # Maker BUY: must be AT or BELOW the best bid (never cross ask)
                retry_price = exchange.round_to_step(bid, tick)
            else:
                # Maker SELL: must be AT or ABOVE the best ask (never cross bid)
                retry_price = exchange.ceil_to_step(ask, tick)

            # Deduplicate clientOrderId on retry
            retry_params = dict(params) if params else {}
            for cid_key in ('clientOrderId', 'newClientOrderId'):
                if cid_key in retry_params:
                    retry_params[cid_key] = f"{retry_params[cid_key]}_R"

            logger.info(
                f"[GTX-RETRY] {label}: Retry {pair} {side} @ {retry_price:.6f} "
                f"(bid={bid:.6f} ask={ask:.6f})"
            )
            try:
                return exchange.create_order(pair, 'limit', side, amount, retry_price, params=retry_params)
            except Exception as e2:
                err2 = str(e2)
                if not _is_postonly_rejected(err2):
                    raise  # Different error — propagate

                # Retry also failed as post-only — market is moving fast.
                # Drop GTX and place a plain limit (taker) as last resort.
                fallback_params = {k: v for k, v in retry_params.items()
                                   if k not in ('postOnly', 'timeInForce')}
                for cid_key in ('clientOrderId', 'newClientOrderId'):
                    if cid_key in fallback_params:
                        fallback_params[cid_key] = f"{fallback_params[cid_key]}_F"
                logger.warning(
                    f"[GTX-FALLBACK] {label}: GTX retry ALSO rejected. "
                    f"Placing plain limit (taker) @ {retry_price:.6f} to avoid silent loss."
                )
                return exchange.create_order(pair, 'limit', side, amount, retry_price, params=fallback_params)


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
            # 🚀 UNIVERSAL PRECISION: Ensure the decayed TP is rounded to exchange tick size
            decayed_tp = strategy._round_price(decayed_tp)
            
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
        Returns the new order dict, or None on failure.
        
        ATOMIC GUARANTEE: If the new placement fails (e.g. GTX rejected because
        EE-decayed price is below market bid), the old DB row is restored to 'open'
        so placed_tp remains anchored to the exchange-live price. This prevents
        the infinite cancel-replace storm that occurs when EE decay produces an
        un-makeable price below current market.
        """
        try:
            tp_order_id = existing_tp_order.get('order_id', existing_tp_order.get('id'))
            
            # 🚀 HARDENED: Verify cancellation before proceeding
            logger.info(f"🔄 [TP-SYNC] {name}: Cancelling stale TP {tp_order_id}...")
            cancel_response = None
            try:
                cancel_response = exchange.cancel_order(tp_order_id, pair)
            except Exception as e:
                logger.warning(f"[TP-SYNC] {name}: Cancel failed ({e}). Attempting to fetch order status...")
                try:
                    cancel_response = exchange.fetch_order(tp_order_id, pair)
                except Exception as inner_e:
                    logger.error(f"[TP-SYNC] {name}: Could not fetch old TP status: {inner_e}")

            # If we successfully obtained the cancelled/current order state, calculate precise remaining quantity
            if cancel_response:
                filled_qty = float(cancel_response.get('filled') or cancel_response.get('executedQty') or 0)
                orig_qty = float(cancel_response.get('amount') or cancel_response.get('origQty') or 0)
                status = str(cancel_response.get('status') or '').lower()
                
                if status in ('closed', 'filled') or (orig_qty > 0 and filled_qty >= orig_qty):
                    logger.warning(f"⚠️ [TP-SYNC] {name}: Old TP {tp_order_id} is FULLY FILLED. Aborting replacement to prevent oversell.")
                    return None
                    
                if orig_qty > 0:
                    calculated_remaining = max(0.0, orig_qty - filled_qty)
                    logger.info(f"✅ [TP-SYNC] {name}: Cancelled order {tp_order_id}. orig: {orig_qty:.4f}, filled: {filled_qty:.4f}, remaining: {calculated_remaining:.4f}")
                    # Update db_qty to the mathematically exact remaining amount
                    db_qty = calculated_remaining

            # Mandatory 500ms safety sleep to allow exchange state to propagate
            time.sleep(0.5)

            # 🚀 ROOT CAUSE FIX: Re-read open_qty from DB after sleep!
            # If the cancelled order was partially or fully filled just before cancellation,
            # the WebSocket will have processed the fill during the 500ms sleep.
            # Using the statically passed db_qty would cause an oversell.
            try:
                from engine.database import get_connection
                _conn = get_connection()
                _latest_qty_row = _conn.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                if _latest_qty_row:
                    _latest_qty = float(_latest_qty_row[0] or 0)
                    if _latest_qty < db_qty:
                        logger.warning(f"⚠️ [TP-SYNC] {name}: open_qty dropped from {db_qty:.4f} to {_latest_qty:.4f} during sleep (WS processed a fill!). Adjusting to prevent oversell.")
                        db_qty = _latest_qty
            except Exception as e:
                logger.error(f"[TP-SYNC] Failed to re-verify open_qty: {e}")

            # Mark cancelled in DB — but remember the old price so we can restore if placement fails
            old_placed_price = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            update_order_status(tp_order_id, 'cancelled', bot_id=bot_id)

            if db_qty <= 0 or db_tp <= 0 or config.DRY_RUN:
                logger.info(f"🛑 [TP-SYNC] {name}: db_qty is {db_qty:.4f} (<= 0) after verification. Aborting replacement.")
                return None

            side = 'sell' if direction == 'LONG' else 'buy'
            valid, db_qty, db_tp, msg = exchange.validate_order(pair, side, db_qty, db_tp, is_closing=True)
            if not valid:
                logger.warning(f"[TP-SYNC] {name}: Validation failed — {msg}")
                # 🚀 ATOMIC RESTORE: Placement can't proceed — restore old row so placed_tp stays valid
                update_order_status(tp_order_id, 'new', bot_id=bot_id)
                return None

            # 🚀 HARDENED: Use cycle_id for strict idempotency
            cycle_id = bot_status.get('cycle_id', 0)
            client_order_id = self._generate_deterministic_id(bot_id, 'TP', cycle_id, bot_status['current_step'])
            tp_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

            logger.info(f"🔄 [TP-SYNC] {name}: Placing IDEMPOTENT TP {client_order_id} @ {db_tp:.4f}...")
            order = self._place_gtx_order_with_retry(
                exchange, pair, side, db_qty, db_tp, params=tp_params, label=f"{name}-TP-SYNC", position_side=direction
            )
            if order:
                save_bot_order(bot_id, 'tp', order['id'], db_tp, db_qty,
                             bot_status['current_step'], order.get('status', 'open'), client_order_id=client_order_id,
                             notes='atomic-sync-post-commit')
                # 🚀 SNAPSHOT HEAL: Inject new order into WS cache so next cycle's snapshot
                # sees the correct price immediately — prevents one-cycle stale-comparison fire.
                try:
                    from engine.ws_cache import get_ws_cache as _gwsc
                    _gwsc().update_order(str(order['id']), order)
                except Exception: pass
                logger.info(f"✅ [SYNC] {name}: Re-placed TP @ {db_tp:.4f} Qty {db_qty:.4f}")
            else:
                # 🚀 ATOMIC RESTORE: GTX was rejected (e.g. EE-decayed price crossed below market bid).
                # Restore the old DB row to 'open' so placed_tp stays anchored to old_placed_price.
                # This stops the false drift loop from firing every cycle.
                logger.warning(
                    f"⚠️ [SYNC] {name}: TP placement failed at exchange (GTX rejected?). "
                    f"Restoring DB row to 'open' — old price {old_placed_price:.4f} preserved as anchor. "
                    f"Will retry next cycle when price allows maker placement."
                )
                update_order_status(tp_order_id, 'new', bot_id=bot_id)
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
        bot_status_str = str(bot_data[12] or '') if len(bot_data) > 12 else ''

        import random
        # 🛡️ JITTER: Add random sleep to desynchronize parallel bots and reduce race conditions
        time.sleep(random.uniform(0.1, 0.8))
        
        # 🚀 MANUAL-GATE PROTECTION: Suspend maintenance if bot requires proof verification
        if 'REQUIRE_MANUAL' in bot_status_str.upper():
            logger.warning(
                f"🛑 [MANUAL-GATE] Bot {name} ({bot_id}) suspended. "
                f"Status='{bot_status_str}'. Proof verification required."
            )
            return None, None

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
            
            # v2.0: TP cascade is now exclusively drained by runner.run_cycle() via
            # ledger.drain_tp_cascade() → handle_tp_completion(). No duplicate drain here.


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
                                   # TWO-TIER: current-cycle first, then historical net fallback.
                                   # Physical exchange positions are cumulative across ALL cycles —
                                   # prior-cycle (reset_cleared) fills still affect the physical qty.
                                   from engine.database import get_connection
                                   sib_net_qty = 0.0
                                   sib_hist_net_qty = 0.0
                                   conn = get_connection()
                                   try:
                                        cursor = conn.cursor()
                                        for b in sibling_bots:
                                            sibling_id = b['bot_id']
                                            # Current-cycle net (standard)
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
                                                AND (cycle_id = (SELECT cycle_id FROM trades WHERE bot_id = ?) OR cycle_id IS NULL)
                                            """, (sibling_id, sibling_id))
                                            row = cursor.fetchone()
                                            q = float(row[0]) if row else 0.0

                                            # Historical net (all cycles, includes reset_cleared fills)
                                            cursor.execute("""
                                                SELECT COALESCE(SUM(
                                                    CASE 
                                                        WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount
                                                        WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -filled_amount
                                                        ELSE 0.0
                                                    END
                                                ), 0.0) 
                                                FROM bot_orders 
                                                WHERE bot_id = ? AND filled_amount > 0
                                            """, (sibling_id,))
                                            hist_row = cursor.fetchone()
                                            hist_q = float(hist_row[0]) if hist_row else 0.0

                                            sign = 1 if b['direction'].upper() == 'LONG' else -1
                                            sib_net_qty += sign * q
                                            sib_hist_net_qty += sign * hist_q
                                   finally:
                                        pass # conn.close() disabled for singleton safety
                                            
                                   # 🚀 Compare actual quantities — two-tier drift check
                                   phys_net_qty_abs = abs(size)
                                   sib_net_qty_abs  = abs(sib_net_qty)
                                   sib_hist_net_qty_abs = abs(sib_hist_net_qty)

                                   # Convert quantity drift back to USD to keep the $ threshold
                                   _mismatch_threshold = exchange.get_symbol_precision(pair).get('min_notional', 5.0)
                                   drift_qty = abs(sib_net_qty_abs - phys_net_qty_abs)
                                   drift_usd = drift_qty * current_price
                                   hist_drift_qty = abs(sib_hist_net_qty_abs - phys_net_qty_abs)
                                   hist_drift_usd = hist_drift_qty * current_price

                                   if drift_usd > _mismatch_threshold:
                                        if hist_drift_usd <= _mismatch_threshold:
                                            # ✅ Historical net explains the gap — prior-cycle accumulation.
                                            logger.info(
                                                f"⚠️ {name}: Current-cycle magnitude mismatch (${drift_usd:.2f} > ${_mismatch_threshold:.2f}) "
                                                f"is explained by cross-cycle history (hist_drift=${hist_drift_usd:.2f}). "
                                                f"Allowing entry — position is valid accumulation."
                                            )
                                            can_enter = True
                                        else:
                                            # BUG-FIX: Check if a recent fill (< 60s ago) explains the gap.
                                            # The async DB-worker runs seal_trade_state after WS fills, so there
                                            # is always a window where the physical position exists on exchange
                                            # but the ledger (total_invested) still reads 0. Without this bypass,
                                            # the very next cycle after a fast fill hits this block and deadlocks.
                                            from engine.database import get_last_filled_order as _glfo
                                            _recent = _glfo(bot_id)
                                            _fill_age = time.time() - float(_recent.get('created_at', 0)) if _recent else 9999
                                            if _fill_age < 90:
                                                logger.info(
                                                    f"⚠️ {name}: Magnitude mismatch (${drift_usd:.2f}) ignored — "
                                                    f"recent fill {_fill_age:.0f}s ago, seal still propagating. Allowing entry."
                                                )
                                                can_enter = True
                                            else:
                                                logger.critical(f"🛑 {name}: Blocked NEW ENTRY! Exchange magnitude {phys_net_qty_abs:.6f} vs System {sib_net_qty_abs:.6f} mismatch ${drift_usd:.2f} > min_notional ${_mismatch_threshold:.2f}. Resolve Mismatch first!")
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
                        trade_update_data = self.execute_entry(bot_id, name, pair, mission['side'], mission['amount'], direction, mission['price'], mission.get('params'), exchange, market_type_snapshot, bot_config, bot_status)
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
            # 🚀 HAMMER SHIELD: Track high-frequency API errors
            tracker = _API_ERROR_TRACKER.setdefault(bot_id, {'count': 0, 'first_time': time.time()})
            tracker['count'] += 1
            if tracker['count'] >= 7:
                elapsed = time.time() - tracker['first_time']
                if elapsed < 20.0:
                    logger.critical(f"🛑 [HAMMER SHIELD] {name} triggered 7 consecutive errors in {elapsed:.1f}s! Auto-deactivating bot to prevent Binance API Ban.")
                    update_bot_error(bot_id, f"HAMMER SHIELD: Auto-deactivated due to rapid API errors. Last: {str(e)[:100]}")
                    try:
                        from engine.database import update_bot_state
                        update_bot_state(bot_id, is_active=0)
                    except: pass
                    tracker['count'] = 0
                else:
                    tracker['count'] = 1
                    tracker['first_time'] = time.time()

            logger.error(f"Error processing bot {name} ({bot_id}): {e}")
            logger.error(traceback.format_exc())
            return None, None # Indicate an error occurred

        # Reset Hammer Shield on successful loop
        if bot_id in _API_ERROR_TRACKER:
            del _API_ERROR_TRACKER[bot_id]

        return 5.0, None

    def execute_entry(self, bot_id, name, pair, side, amount, direction, price=None, params=None, exchange=None, market_snapshot=None, bot_config=None, bot_status=None) -> Optional[Dict[str, Any]]:
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

        # 🚀 CARRY_PENDING GUARD
        # Do NOT place new entry orders if we are waiting for a dust/carry position
        # to be adopted by the background Reconciler process.
        if bot_status.get('cycle_phase') == 'CARRY_PENDING':
             logger.info(f"⏳ [CARRY-PENDING] {name}: Awaiting carry adoption from Reconciler. Suspending ENTRY order placement.")
             return None

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

        # 2. In-Flight Buffer: Check basket_start within 30s window
        basket_start = bot_status.get('basket_start_time', 0)
        if basket_start and (time.time() - basket_start) < 30.0:
             logger.warning(f"🛡️ {name}: Entry attempt IN-FLIGHT ({time.time() - basket_start:.1f}s ago). Blocking double-tap.")
             return None

        # 3. ── DB ENTRY ANCHOR GUARD ─────────────────────────────────────────────
        # Even after 30s basket expiry, check bot_orders for any live/filled entry row.
        # This catches the fill-credit miss case where:
        #   - entry filled on exchange
        #   - DB row exists (save_bot_order was called) but credit_fill failed
        #   - 30s lock expired → bot tries to place ANOTHER entry
        # Solution: retroactively credit the fill from DB, never spam a new order.
        try:
            from engine.database import get_connection as _gc
            _conn = _gc()
            # ── ANCHOR GUARD v2: scope to CURRENT cycle_id only ─────────────────────
            # BUG-FIX: Without cycle_id scoping, historical filled rows from prior cycles
            # remain in bot_orders (status='filled', not reset_cleared) and permanently
            # trigger the anchor, deadlocking the bot even after a clean wipe+cycle-bump.
            _cur_cycle_row = _conn.execute(
                "SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id=?", (bot_id,)
            ).fetchone()
            _cur_cycle = _cur_cycle_row[0] if _cur_cycle_row else 1

            # Only look at entry rows from the CURRENT cycle that are not archived
            _live_entries = _conn.execute("""
                SELECT order_id, client_order_id, filled_amount, price, status
                FROM bot_orders
                WHERE bot_id = ?
                  AND cycle_id = ?
                  AND order_type = 'entry'
                  AND status NOT IN ('reset_cleared', 'auto_closed')
                  AND (filled_amount > 0 OR status NOT IN ('cancelled', 'canceled', 'failed'))
                ORDER BY id DESC LIMIT 5
            """, (bot_id, _cur_cycle)).fetchall()

            if _live_entries:
                # Check if any are filled but not credited
                for _row in _live_entries:
                    _oid, _cid, _filled, _px, _status = _row
                    if _filled and float(_filled) > 0 and float(bot_status.get('total_invested', 0)) <= 0:
                        from engine.ledger import credit_fill, seal_trade_state
                        _ok = credit_fill(bot_id, str(_oid), float(_filled), float(_px), 'entry', is_cumulative=True)
                        if _ok:
                            seal_trade_state(bot_id)
                            logger.warning(
                                f"[ENTRY-ANCHOR] Bot {name}: Recovered uncredited fill from bot_orders "
                                f"(order={_oid} filled={_filled} px={_px}). Blocking new entry."
                            )
                            return None

                # WS-lag check: if there's a non-filled entry row not in open_orders,
                # it may be mid-fill. Block to avoid duplicate.
                # BUG-FIX: Do NOT block if the order is already status='filled'/'closed' —
                # filled orders are correctly absent from open_orders (they're done).
                # Blocking on a filled+credited order causes a permanent deadlock.
                _newest = _live_entries[0]
                _newest_oid = str(_newest[0])
                _newest_status = str(_newest[4]).lower()
                _seen_ids = {str(o['id']) for o in bot_open_orders}
                if _newest_oid not in _seen_ids and _newest_status not in ('filled', 'closed'):
                    logger.warning(
                        f"[ENTRY-ANCHOR] Bot {name}: DB has live entry {_newest_oid} "
                        f"(status={_newest[4]}) not in open_orders snapshot. "
                        f"Blocking new entry — may be WS lag or mid-fill."
                    )
                    return None
        except Exception as _anchor_err:
            logger.warning(f"[ENTRY-ANCHOR] Bot {name}: guard check failed (non-blocking): {_anchor_err}")
        # ─────────────────────────────────────────────────────────────────────────



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
                        # ──────────────────────────────────────────────────────────────────
                        # MAKER-PRICE RE-ALIGNMENT (Root Cause Fix)
                        # ──────────────────────────────────────────────────────────────────
                        # `price` from decide_action() is the LAST traded price — not the
                        # current bid or ask. Placing a Post-Only sell at the bid (or a buy
                        # at the ask) immediately crosses the spread and gets -5022 rejected.
                        #
                        # The previous fix read bid/ask from the runner snapshot ticker, but
                        # the snapshot tickers dict is keyed by normalized symbol (SOLUSDC)
                        # while `pair` is the CCXT symbol (SOL/USDC:USDC). On a key-miss,
                        # both bid and ask defaulted back to `price` (last traded), so the
                        # alignment condition never fired and the GTX rejection loop repeated
                        # every 60s (chase cancel → re-enter at last price → reject → repeat).
                        #
                        # Fix: always fetch LIVE bid/ask from exchange before placement.
                        # This is the same "use current best bid/ask" logic used for offline
                        # fills: when price has passed the original target, just use the best
                        # available maker price on the correct side right now.
                        # ──────────────────────────────────────────────────────────────────
                        live_bid, live_ask = exchange.get_best_bid_ask(pair)
                        if live_bid and live_ask and live_bid > 0 and live_ask > 0:
                            prec_info = exchange.get_symbol_precision(pair)
                            tick = prec_info.get('tick_size', 0.0001)
                            if side.lower() == 'buy':
                                # Maker BUY: must sit at or below best bid (never cross ask)
                                aligned = exchange.round_to_step(live_bid, tick)
                                if price >= live_ask or abs(price - aligned) / max(aligned, 1e-9) > 0.0001:
                                    logger.info(f"🛡️ {name}: Aligning LONG Maker Entry {price:.6f} → Best Bid {aligned:.6f} (bid={live_bid:.6f} ask={live_ask:.6f})")
                                    price = aligned
                            else:  # sell (SHORT entry)
                                # Maker SELL: must sit at or above best ask (never cross bid)
                                aligned = exchange.ceil_to_step(live_ask, tick)
                                if price <= live_bid or abs(price - aligned) / max(aligned, 1e-9) > 0.0001:
                                    logger.info(f"🛡️ {name}: Aligning SHORT Maker Entry {price:.6f} → Best Ask {aligned:.6f} (bid={live_bid:.6f} ask={live_ask:.6f})")
                                    price = aligned
                        else:
                            logger.warning(f"⚠️ {name}: Could not fetch live bid/ask for maker alignment. Using strategy price {price:.6f}.")
                    except Exception as e:
                        logger.error(f"⚠️ {name}: Maker alignment error: {e}")

                    valid, amount, price, msg = exchange.validate_order(pair, side, amount, price)
                    if not valid:
                        logger.error(f"❌ Entry Order validation failed for {name} {pair}: {msg}")
                        update_bot_error(bot_id, f"Entry Order validation failed: {msg}")
                        return

                    logger.info(f"🧐 {name}: Creating Order on Exchange...")
                    cycle_id = bot_status.get('cycle_id', 0)
                    client_order_id = self._generate_deterministic_id(bot_id, 'ENTRY', cycle_id, 1)
                    
                    # 🚀 CONCURRENCY LOCK: Set basket_start_time BEFORE calling exchange
                    # This prevents rapid-fire loops from bypassing the in-flight check.
                    try:
                        from engine.database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE trades SET basket_start_time = ? WHERE bot_id = ?", (int(time.time()), bot_id))
                        cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))
                        conn.commit()
                    except Exception as lock_err:
                        logger.error(f"❌ {name}: Failed to set concurrency lock: {lock_err}")

                    # 🚀 SPREAD-CROSS FALLBACK
                    ccxt_entry_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

                    order = self._place_gtx_order_with_retry(exchange, pair, side, amount, price, params=ccxt_entry_params, label=f"{name}-ENTRY", position_side=direction)
                    
                    if order:
                        # RECORD IN BOT_ORDERS (atomic, after exchange confirms)
                        save_bot_order(bot_id, 'entry', order['id'], price, amount,
                                       1, order.get('status', 'open'), client_order_id=client_order_id, notes='atomic-post-commit')

                        # ── RETROACTIVE FILL GUARD ──────────────────────────────────────
                        # If exchange returned order already filled/partial, the WS event
                        # may have fired BEFORE save_bot_order created the row (race).
                        # Credit it here immediately so DB reflects reality without waiting.
                        order_status = str(order.get('status', '')).lower()
                        order_filled = float(order.get('filled', 0) or 0)
                        if order_status in ('filled', 'closed') and order_filled <= 0:
                            order_filled = float(order.get('amount') or 0)
                        if order_status in ('filled', 'closed', 'partially_filled') and order_filled > 0:
                            try:
                                from engine.ledger import credit_fill, seal_trade_state
                                order_avg = float(order.get('average') or order.get('price') or price)
                                credited = credit_fill(
                                    bot_id=bot_id,
                                    order_id=str(order['id']),
                                    cumulative_qty=order_filled,
                                    avg_price=order_avg,
                                    order_type='entry',
                                    is_cumulative=True
                                )
                                if credited:
                                    seal_trade_state(bot_id)
                                    logger.info(
                                        f"[ENTRY-RETRO] Bot {name}: order {order['id']} already "
                                        f"{order_status} ({order_filled:.6f} filled). "
                                        f"Retroactive credit_fill + seal done."
                                    )
                            except Exception as retro_err:
                                logger.warning(f"[ENTRY-RETRO] Bot {name}: retroactive fill failed: {retro_err}")
                        # ────────────────────────────────────────────────────────────────

                        # Record entry_order_id in trades for quick lookup
                        try:
                            from engine.database import get_connection
                            conn = get_connection()
                            conn.execute(
                                "UPDATE trades SET entry_order_id = ? WHERE bot_id = ?",
                                (order['id'], bot_id)
                            )
                            conn.commit()
                            logger.info(f"[ENTRY] Bot {name}: order {order['id']} recorded in DB.")
                            update_bot_error(bot_id, None)
                        except Exception as db_err:
                            logger.error(f"[ENTRY] Bot {name}: failed DB update: {db_err}")
                            update_bot_error(bot_id, f"DB update error after entry: {db_err}")

                        return None

                    else:
                        # Order failed at exchange, no DB entry was made, no manual rollback needed
                        logger.warning(f"⚠️ {name}: Entry order failed at exchange level. Ledger remains clean.")
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
                        pass # conn.close() disabled for singleton safety
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
                    pass # conn.close() disabled for singleton safety
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
                pass # conn.close() disabled for singleton safety
                return

            cur.execute(
                "SELECT SUM(amount * price) / SUM(amount), SUM(amount), side FROM bot_orders "
                "WHERE bot_id=? AND order_type='hedge' AND status='filled' GROUP BY side",
                (bot_id,)
            )
            res = cur.fetchone()
            pass # conn.close() disabled for singleton safety
            
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
                
                cycle_id = bot_status.get('cycle_id', 0)
                cid = self._generate_deterministic_id(bot_id, 'HEDGETP', cycle_id, 0)
                params = {'timeInForce': 'GTX', 'postOnly': True, 'newClientOrderId': cid}
                
                if not config.TRADING_ENABLED and not config.DRY_RUN:
                    return
                
                order = None
                if config.TRADING_ENABLED:
                    try:
                        order = self._place_gtx_order_with_retry(
                            exchange, pair, exit_side, hedge_qty, be_price, params, label=f"HEDGE-EXIT-{name}", position_side=hedge_side
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
                                            AND bot_id != ? AND pair = ? AND position_side = ?
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
                    save_bot_order(
                        bot_id,
                        'hedge_tp',
                        str(order['id']),
                        be_price,
                        hedge_qty,
                        0,  # step: hedge_tp orders are not step-indexed
                        'open',
                        params.get('clientOrderId', params.get('newClientOrderId', cid)),
                        None,  # notes
                        exit_side  # position_side
                    )
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

        # CASE 1: CARRY_PENDING GUARD -> SUSPEND MAINTENANCE
        # A bot in CARRY_PENDING state has TP'd but left a remainder dust position.
        # It is waiting for the background Reconciler to adopt the remainder into the ledger.
        # Do NOT treat it as "SCANNING" (which would purge orders) and do NOT place TP/Grid.
        if bot_status.get('cycle_phase') == 'CARRY_PENDING':
             logger.info(f"⏳ [CARRY-PENDING] {name}: Ledger awaiting background carry adoption. Suspending maintenance.")
             return None

        # CASE 2: IN TRADE -> NO ENTRY ORDERS ALLOWED
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
        if bot_status['total_invested'] == 0 and bot_status['current_step'] == 0:
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

        # 🚀 STEP-SYNC FIX (Deterministic ID Parsing)
        # ID Format: CQB_{bot_id}_{prefix}_{cycle_id}_{step}
        # e.g., CQB_10018_GRID_0_3 means grid for step 3.
        current_step = bot_status['current_step']
        expected_tp_step = current_step
        expected_grid_step = current_step + 1

        def get_step_from_cid(cid: str, prefix: str) -> int:
            """Extracts the step integer from a CQB clientOrderId. Returns -1 if invalid."""
            try:
                # CQB_100_GRID_0_3 -> split by _GRID_ -> "0_3"
                parts = cid.split(f"_{prefix}_")
                if len(parts) > 1:
                    # The remainder is "{cycle_id}_{step}" maybe with "_R" retry suffix
                    remainder = parts[1].split('_')
                    # remainder[0] is cycle_id, remainder[1] is step
                    if len(remainder) >= 2:
                        # strip any alpha suffixes like R or F before int conversion, though they append with _, so split drops it
                        return int(remainder[1].replace('R','').replace('F',''))
            except: pass
            return -1

        stored_tp_id = str(bot_status.get('tp_order_id', '') or '')
        
        valid_tp_orders = []
        valid_grid_orders = []
        stale_orders = []

        for o in tp_orders:
            cid = o.get('clientOrderId', '')
            step_num = get_step_from_cid(cid, 'TP')
            if step_num == expected_tp_step or (stored_tp_id and o.get('id', '') == stored_tp_id):
                valid_tp_orders.append(o)
            elif step_num != -1 and step_num < expected_tp_step:
                stale_orders.append(o)
            elif step_num == -1: # Fallback for malformed or manual orders
                stale_orders.append(o)

        for o in grid_orders:
            cid = o.get('clientOrderId', '')
            step_num = get_step_from_cid(cid, 'GRID')
            if step_num == expected_grid_step:
                valid_grid_orders.append(o)
            elif step_num != -1 and step_num < expected_grid_step:
                stale_orders.append(o)
            elif step_num == -1:
                stale_orders.append(o)
                
        for o in dust_orders:
            cid = o.get('clientOrderId', '')
            step_num = get_step_from_cid(cid, 'DUST')
            if step_num != -1 and step_num < current_step:
                stale_orders.append(o)
            elif step_num == -1:
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
            grid_orders.sort(key=lambda x: 1 if get_step_from_cid(x.get('clientOrderId', ''), 'GRID') == expected_grid_step else 0, reverse=True)
            for o in grid_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except: pass
            valid_grid_orders = [grid_orders[0]] if get_step_from_cid(grid_orders[0].get('clientOrderId',''), 'GRID') == expected_grid_step else []
            existing_grid_order = grid_orders[0]
        else:
            existing_grid_order = valid_grid_orders[0] if valid_grid_orders else None

        if len(tp_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(tp_orders)} total TP orders. Restricting to strict 1 max (Sweeping Ghosts)...")
            # Sort to prefer the matching step, otherwise just keep newest
            tp_orders.sort(key=lambda x: 1 if get_step_from_cid(x.get('clientOrderId', ''), 'TP') == expected_tp_step or (stored_tp_id and x.get('id', '') == stored_tp_id) else 0, reverse=True)
            for o in tp_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except: pass
            valid_tp_orders = [tp_orders[0]] if get_step_from_cid(tp_orders[0].get('clientOrderId',''), 'TP') == expected_tp_step or (stored_tp_id and tp_orders[0].get('id', '') == stored_tp_id) else []
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
                            
                            # v2.0: Register in cascade registry — runner will do atomic cancel+reset
                            from engine.ledger import register_tp_cascade, credit_fill as _cf_tp
                            
                            # 🚀 ROOT CAUSE FIX (v2.1.1): Extract REST fill timestamp and pass to cascade
                            # Without this, REST-detected TP fills resulted in cycle_start_time=0,
                            # breaking the cycle poisoning guard on the next restart.
                            _rest_ts = order_status.get('lastTradeTimestamp') or order_status.get('timestamp') or (time.time() * 1000)
                            _exit_fill_ts = int(_rest_ts / 1000)
                            
                            _cf_tp(bot_id=bot_id, order_id=str(local_tp_id),
                                   cumulative_qty=filled_amount, avg_price=actual_exit,
                                   order_type='tp', is_cumulative=True)
                            register_tp_cascade(bot_id, pair, actual_exit, _exit_fill_ts)
                            logger.info(f"[TP-EVICTOR] {name}: REST-detected TP fill registered for cascade (ts={_exit_fill_ts}). Runner will complete reset.")
                        else:
                            logger.debug(f"⏭️ {name}: Stored TP ID {local_tp_id} is FILLED, but bot state already zeroed. Skipping.")
                        return None # Exit cycle
                    elif status_str in ['new', 'open', 'partially_filled']:
                         # 🚀 SNAPSHOT LAG FIX: The order IS confirmed live on exchange (status=new).
                         # It's simply absent from the stale start-of-cycle snapshot.
                         # Back-populate the WS cache so next cycle sees it and skip eviction.
                         logger.info(f"✅ {name}: Stored TP {local_tp_id} is CONFIRMED LIVE (status={status_str}) — snapshot was stale. Healing cache.")
                         from engine.ws_cache import get_ws_cache as _gwsc
                         _gwsc().update_order(str(local_tp_id), order_status)
                         # Do NOT modify local_tp_id — keep the lock so we don't re-place a duplicate
                    else:
                         # Truly unrecognised status — treat as ghost and evict.
                         logger.warning(f"⏳ {name}: Stored TP {local_tp_id} status is {status_str}, unrecognised. Forcing CANCEL and Eviction.")
                         try:
                             exchange.cancel_order(local_tp_id, pair)
                         except: pass
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         _uos(local_tp_id, 'cancelled', bot_id=bot_id)
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
            
                # 🚀 SPREAD-CROSSING MAKER LOOP FIX (Root Cause of Flashing)
                # If a Post-Only (GTX) limit order crosses the active spread, Binance accepts the API payload 
                # but silently/instantly cancels it in the matching engine (status becomes EXPIRED). 
                # This caused the engine to endlessly re-place the order every cycle, causing UI flashes.
                # Standard TP: LONG bot sells to close. Must be >= Best Ask to remain Maker.
                #              SHORT bot buys to close. Must be <= Best Bid to remain Maker.
                try:
                    bid, ask = exchange.get_best_bid_ask(pair)
                    if bid is not None and ask is not None:
                        bid_val = float(bid)
                        ask_val = float(ask)
                        if direction == 'LONG':
                            # We are Selling to close. If TP is lower than or equals the best bid, it crosses the spread and acts as Taker.
                            if tp_price <= bid_val:
                                old_tp = tp_price
                                tp_price = ask_val # Join the asks to stay Maker
                                logger.info(f"🚀 {name}: TP Spread Cross Prevented! (Sell {old_tp} <= Bid {bid_val}). Adjusted to Ask {tp_price} to preserve GTX.")
                        else:
                            # We are Buying to close. If TP is higher than or equals the best ask, it crosses the spread and acts as Taker.
                            if tp_price >= ask_val:
                                old_tp = tp_price
                                tp_price = bid_val # Join the bids to stay Maker
                                logger.info(f"🚀 {name}: TP Spread Cross Prevented! (Buy {old_tp} >= Ask {ask_val}). Adjusted to Bid {tp_price} to preserve GTX.")
                except Exception as e:
                    logger.warning(f"⚠️ {name}: Market Gap check failed ({e}). Proceeding without spread-cross protection.")

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
                                cycle_id = bot_status.get('cycle_id', 0)
                                client_order_id = self._generate_deterministic_id(bot_id, 'TP', cycle_id, bot_status['current_step'])
                                side = 'sell' if direction == 'LONG' else 'buy'
                                
                                # 🚀 Unified TP Param Preparation
                                ccxt_params, tp_amount = self._prepare_tp_order_params(
                                    bot_id, name, pair, side, tp_amount, tp_price, current_price, exchange, direction
                                )
                                if ccxt_params is None:
                                    pass  # Early exit handled inside _prepare_tp_order_params, but inside inside place block we just let it skip
                                elif ccxt_params == 'DUST_CHASER':
                                    # 🚀 MARKET DUST FLUSH: Multi-bot pair, sub-threshold virtual position.
                                    # A limit TP is impossible (min notional rejection, no reduceOnly allowed).
                                    # Correct architecture: fire a net-REDUCING market order to zero the virtual position.
                                    # In One-Way mode, this is always safe: it's just netting against the pair's physical position.
                                    # The CID tags it to this bot only — sibling bots are fully unaffected.
                                    logger.warning(f"🧹 [DUST-FLUSH] {name}: Virtual position ${bot_status.get('total_invested', 0):.2f} below min notional. Firing market dust-close.")
                                    try:
                                        dust_qty = tp_amount  # qty returned from _prepare_tp_order_params
                                        dust_side = 'sell' if direction == 'LONG' else 'buy'
                                        dust_cid = self._generate_deterministic_id(bot_id, 'DUST', bot_status.get('cycle_id', 0), bot_status.get('current_step', 1))
                                        
                                        # Market close — no price needed, taker execution
                                        # 🚀 FIX: Add reduceOnly=True to bypass MIN_NOTIONAL filter for sub-$5 orders
                                        dust_order = exchange.create_order(
                                            pair, 'market', dust_side, dust_qty,
                                            params={'newClientOrderId': dust_cid, 'reduceOnly': True}
                                        )
                                        
                                        if dust_order:
                                            dust_fill_price = float(dust_order.get('average') or dust_order.get('price') or current_price)
                                            logger.info(f"✅ [DUST-FLUSH] {name}: Market close executed. ID={dust_order['id']} qty={dust_qty} @ ~{dust_fill_price:.4f}")
                                            
                                            # Credit the fill and seal the cycle
                                            from engine.ledger import credit_fill as _cf_dust, seal_trade_state as _sts_dust
                                            from engine.database import update_order_status as _uos_dust
                                            _credited = _cf_dust(
                                                bot_id=bot_id,
                                                order_id=str(dust_order['id']),
                                                cumulative_qty=dust_qty,
                                                avg_price=dust_fill_price,
                                                order_type='tp',
                                                is_cumulative=True
                                            )
                                            _uos_dust(dust_order['id'], 'filled', bot_id=bot_id, filled_qty=dust_qty)
                                            if _credited:
                                                _sts_dust(bot_id)
                                            logger.info(f"✅ [DUST-FLUSH] {name}: Ledger sealed. Bot will resume scanning next cycle.")
                                    except Exception as e_dust:
                                        logger.error(f"❌ [DUST-FLUSH] {name}: Market dust-close failed: {e_dust}. Bot may require manual intervention.")
                                else:
                                    # ---------------------------------
                                    # STANDARD TP PLACEMENT
                                    # ---------------------------------
                                    # 🔑 CRITICAL FIX: Embed the CQB_ clientOrderId so that
                                    # maintain_orders can find this TP in the next open_orders fetch.
                                    ccxt_params['newClientOrderId'] = client_order_id

                                    order = self._place_gtx_order_with_retry(exchange, pair, side, tp_amount, tp_price, params=ccxt_params, label=f"{name}-MAINTAIN-TP", position_side=direction)
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

            # ── STEP 1: Run EE decay to detect if a NEW interval has fired.
            # _compute_effective_tp only returns a *different* value from raw_db_tp
            # when math.floor(duration_mins / interval_mins) has incremented.
            # If the interval hasn't changed it returns exactly raw_db_tp.
            new_ee_tp = self._compute_effective_tp(bot_id, name, bot_status, bot_config, strategy)

            # ── STEP 2: DRIFT CHECK — compare what Binance actually holds
            # against what we PHYSICALLY PLACED (bot_orders.price), NOT a
            # freshly re-computed value.  Using a re-computed value was the
            # root cause of false SYNC-DRIFT fires: avg_entry_price can shift
            # between cycles (grid fills), making the re-computed base TP
            # differ from the placed TP even when no EE interval has elapsed.
            exchange_tp  = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            exchange_qty = self._get_order_amount(existing_tp_order)

            # Read the price we PLACED from bot_orders (the authoritative record).
            placed_tp = 0.0
            try:
                from engine.database import get_connection as _gc_tp_check
                with _gc_tp_check() as _c_chk:
                    _tp_row = _c_chk.execute(
                        "SELECT price FROM bot_orders WHERE bot_id=? AND order_type='tp'"
                        " AND status IN ('open','new','placed') ORDER BY created_at DESC LIMIT 1",
                        (bot_id,)
                    ).fetchone()
                    if _tp_row and _tp_row[0]:
                        placed_tp = float(_tp_row[0])
            except Exception as _e_tp_chk:
                logger.debug(f"[SYNC-DRIFT] {name}: Could not read placed_tp from bot_orders: {_e_tp_chk}")

            # Fall back to DB target_tp_price if bot_orders row is missing (e.g. legacy bot).
            if placed_tp <= 0:
                placed_tp = float(bot_status.get('target_tp_price', 0))

            # DB-TP ZERO GUARD: recalculate only if we genuinely have no reference
            if placed_tp == 0 and bot_status.get('avg_entry_price', 0) > 0:
                placed_tp = strategy.calculate_take_profit_price(bot_status, bot_status.get('avg_entry_price', 0))
                logger.info(f"[TP-RECOVER] {name}: placed_tp was 0, recalculated to {placed_tp:.4f} from avg_entry.")

            # ── STEP 3: Decide whether to replace the TP on exchange.
            # Case A: EE fired a new interval → must replace with new_ee_tp.
            # Case B: Genuine price mismatch (e.g. position size changed, TP
            #         was cancelled/refilled and re-placed at wrong price).
            #
            # Tolerance: 0.1% — only covers exchange rounding noise (tick_size).
            # We do NOT need wider tolerance because we compare placed vs live,
            # not recomputed vs live.
            db_qty = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            valid, db_qty, _, _ = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', db_qty, placed_tp, is_closing=True)

            ee_interval_fired = (new_ee_tp != placed_tp and new_ee_tp > 0 and placed_tp > 0)
            if exchange_tp > 0 and placed_tp > 0:
                drift_tp  = abs(placed_tp - exchange_tp) / max(placed_tp, 0.01)
                drift_qty = abs(db_qty   - exchange_qty) / max(db_qty, 0.0001)

                tp_tolerance  = 0.001   # 0.1% — rounding noise only (not a patch)
                qty_tolerance = 0.05    # 5% for lot-size step rounding on small positions

                price_drifted = drift_tp > tp_tolerance
                qty_drifted   = drift_qty > qty_tolerance

                if ee_interval_fired or price_drifted or qty_drifted:
                    replace_reason = []
                    if ee_interval_fired:
                        replace_reason.append(f"EE-stepped {placed_tp:.4f}→{new_ee_tp:.4f}")
                    if price_drifted:
                        replace_reason.append(f"price-drift {drift_tp*100:.4f}% (placed:{placed_tp:.4f} live:{exchange_tp:.4f})")
                    if qty_drifted:
                        replace_reason.append(f"qty-drift {drift_qty*100:.2f}% (want:{db_qty:.4f} live:{exchange_qty:.4f})")
                    logger.info(f"[SYNC-DRIFT] {name}: Replacing TP — {'; '.join(replace_reason)}")

                    # Use the EE-updated price if a new interval fired, else the placed price
                    target_tp = new_ee_tp if ee_interval_fired else placed_tp
                    tp_order = self._sync_replace_tp(
                        bot_id, name, pair, direction, bot_status, exchange,
                        target_tp, db_qty, existing_tp_order
                    )


        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps:
             # 🚀 GRID IDEMPOTENCY LOCK: Absolute State Enforcement
             # Check DB for ANY proof that we already placed this exact step in this cycle.
             # We check all active/terminal statuses, trusting the DB truth over lagging exchange sync.
             try:
                 from engine.database import get_connection
                 _conn = get_connection()
                 _placed_grid = _conn.execute(
                     "SELECT 1 FROM bot_orders WHERE bot_id=? AND cycle_id=? AND step=? AND order_type='grid' AND status IN ('placing', 'new', 'open', 'partially_filled', 'filled', 'closed')", 
                     (bot_id, bot_status.get('cycle_id', 0), expected_grid_step)
                 ).fetchone()
                 if _placed_grid:
                     logger.warning(f"🛡️ {name}: DB mathematically proves Grid step {expected_grid_step} is ALREADY placed/filled. Yielding Grid placement to prevent double-tap.")
                     return None
             except: pass

             # 🚀 STRICT SEQUENCING: Do NOT place Grid orders if an Entry order is still open.
             if existing_entry_orders:
                  logger.info(f"⏳ {name}: Entry order is still open. Waiting for Full Fill before placing Grid Orders.")
                  return None

             # v2.0: Physical-size drift is surfaced as a warning alert only.
             # Grid placement is NOT blocked by drift (Rule 5 of canonical architecture).
             # Risk is managed by circuit breaker and per-bot position limits.
             try:
                 phys_positions = market_snapshot.get('positions', []) if market_snapshot else []
                 phys_net_signed = 0.0  # signed: positive=LONG, negative=SHORT
                 for p in phys_positions:
                     if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                         # 🚀 ONE-WAY MODE FIX: Binance One-Way always returns side='both'.
                         # The ONLY reliable signal is the SIGN of contracts:
                         #   positive → net LONG position, negative → net SHORT position
                         raw_contracts = float(p.get('contracts', 0) or 0)
                         if raw_contracts != 0:
                             phys_net_signed = raw_contracts

                 virtual_qty = (float(bot_status.get('total_invested', 0) or 0) /
                                float(bot_status.get('avg_entry_price', 1) or 1))

                 # 🚀 PAIR-CONSENSUS FIX: In One-Way mode, two bots on the same pair
                 # (one LONG, one SHORT) net out at the exchange. The physical position
                 # for this bot = this_bot_virtual - sibling_bot_virtual (opposite side).
                 # We must account for the sibling's virtual qty before raising a drift alert.
                 sibling_virtual = 0.0
                 try:
                     from engine.database import get_connection as _gc_drift
                     with _gc_drift() as _c_drift:
                         opp_dir = 'SHORT' if direction == 'LONG' else 'LONG'
                         _sib = _c_drift.execute("""
                             SELECT t.total_invested, t.avg_entry_price
                             FROM bots b JOIN trades t ON t.bot_id=b.id
                             WHERE b.pair=? AND b.direction=? AND b.is_active=1
                               AND t.total_invested > 0.01
                         """, (pair, opp_dir)).fetchone()
                         if _sib:
                             _sib_inv = float(_sib[0] or 0)
                             _sib_avg = float(_sib[1] or 1)
                             sibling_virtual = _sib_inv / _sib_avg if _sib_avg > 0 else 0.0
                 except Exception:
                     pass

                 # Expected net contribution from this bot: our virtual ± sibling's virtual
                 # LONG bot: expected physical = (long_virtual - short_sibling_virtual)
                 # SHORT bot: expected physical = (short_virtual - long_sibling_virtual) → negated
                 if direction == 'LONG':
                     expected_net = virtual_qty - sibling_virtual
                     actual_net = phys_net_signed  # positive = long
                 else:
                     expected_net = -(virtual_qty - sibling_virtual)
                     actual_net = phys_net_signed  # negative = short

                 drift_qty = abs(actual_net - expected_net)
                 drift_pct = drift_qty / max(abs(expected_net), 0.001)

                 if virtual_qty > 0.001 and drift_pct > 0.10:
                     logger.warning(
                         f"[DRIFT-ALERT] {name}: physical_net={actual_net:.4f} vs "
                         f"expected_net={expected_net:.4f} "
                         f"(this_virt={virtual_qty:.4f} sibling_virt={sibling_virtual:.4f} "
                         f"diff={drift_qty:.4f} {drift_pct*100:.1f}%). "
                         f"Monitor parity — grid placement continues normally (v2.0)."
                     )
             except Exception as _drift_err:
                 logger.debug(f"Drift alert check failed for {name}: {_drift_err}")




             # ══════════════════════════════════════════════════════════════
             # STEP-PROGRESSION-PROOF  (3-Tier, Self-Healing)
             # ══════════════════════════════════════════════════════════════
             # Tier 1: entry_confirmed DB flag (set by seal_trade_state / WS)
             # Tier 2: bot_orders filled row for current_step
             # Tier 3: Math proof — total_invested > 0 AND avg_entry_price > 0
             #         → position is provably real; auto-heal entry_confirmed=1
             #         so the deadlock never recurs.
             #
             # WHY a math fallback:
             #   bot_orders rows can be missing after DB migration, engine restarts
             #   during a fill, or when the position was adopted from the exchange
             #   (forensic / manual import paths). The trades table math
             #   (total_invested / avg_entry_price) is ALWAYS the ground truth;
             #   if it says we hold a position, we trust it unconditionally.
             # ══════════════════════════════════════════════════════════════
             if bot_status['current_step'] > 0:
                 try:
                     _ec = bot_status.get('entry_confirmed', 0)
                     _invested = float(bot_status.get('total_invested', 0) or 0)
                     _avg      = float(bot_status.get('avg_entry_price', 0) or 0)

                     # ── Tier 1: entry_confirmed flag ──────────────────────
                     if _ec == 1:
                         logger.debug(f"🛡️ {name}: Step proof T1 passed (entry_confirmed=1).")

                     else:
                         # ── Tier 2: bot_orders filled row ─────────────────
                         from engine.database import get_connection as _gcsp
                         _csp = _gcsp()
                         _row = _csp.execute("""
                             SELECT COUNT(*) FROM bot_orders
                             WHERE bot_id=? AND status IN ('filled','closed')
                               AND step=? AND created_at >= (? - 2592000)
                         """, (bot_id, bot_status['current_step'],
                               bot_status.get('basket_start_time', 0))).fetchone()

                         if _row and _row[0] > 0:
                             logger.debug(f"🛡️ {name}: Step proof T2 passed (bot_orders filled row found).")
                             # Promote to T1 so next cycle skips this query
                             try:
                                 _csp.execute(
                                     "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (bot_id,)
                                 )
                                 _csp.commit()
                             except Exception: pass

                         elif _invested > 0.01 and _avg > 0:
                             # ── Tier 3: Math proof — auto-heal ────────────
                             # total_invested and avg_entry_price are non-zero,
                             # meaning seal_trade_state already wrote the ledger
                             # from real fills. The bot_orders row is simply missing
                             # (migration, adoption, restart). Trust the math.
                             logger.warning(
                                 f"🩹 [PROOF-T3] {name}: bot_orders fill record absent for step "
                                 f"{bot_status['current_step']} but math proves position "
                                 f"(invested=${_invested:.2f} avg={_avg:.4f}). "
                                 f"Auto-healing entry_confirmed=1."
                             )
                             try:
                                 _csp.execute(
                                     "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (bot_id,)
                                 )
                                 _csp.commit()
                             except Exception as _heal_e:
                                 logger.error(f"[PROOF-T3] {name}: Failed to write entry_confirmed: {_heal_e}")
                             # Continue to grid placement — proof accepted

                         else:
                             # All 3 tiers failed: no flag, no order row, no math.
                             # The bot genuinely has no fill proof — block and wait.
                             logger.warning(
                                 f"🛑 {name}: Step Progression Blocked (all 3 proof tiers failed). "
                                 f"step={bot_status['current_step']} invested=${_invested:.4f} "
                                 f"entry_confirmed={_ec}. Waiting for reconciler/WS."
                             )
                             return None

                 except Exception as e:
                     logger.error(f"❌ {name}: Step progression proof raised exception: {e}")
                     # On unexpected error: fall through ONLY if math says we hold
                     _invested = float(bot_status.get('total_invested', 0) or 0)
                     _avg      = float(bot_status.get('avg_entry_price', 0) or 0)
                     if _invested > 0.01 and _avg > 0:
                         logger.warning(f"⚠️ {name}: Proof exception but math confirms position. Continuing.")
                     else:
                         logger.warning(f"🛑 {name}: Proof exception and no math backup. Blocking grid.")
                         return None


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
                     update_bot_error(bot_id, f"CONFIG ERROR: Base Size (${bot_config.get('base_size',0):.2f}) < Min Notional (${exchange_min_notional:.2f})")
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

                      if status_str in ['filled', 'closed']:
                           actual_fill_qty = float(order_status.get('filled', 0) or 0)
                           actual_fill_price = float(order_status.get('average') or order_status.get('price') or 0)
                           # 🔧 Demo FAPI returns average=0 for filled orders — fall back to stored grid price
                           if actual_fill_price <= 0:
                               try:
                                   from engine.database import get_connection as _gcnn
                                   _fb_conn = _gcnn()
                                   _fb_row = _fb_conn.execute(
                                       "SELECT price FROM bot_orders WHERE order_id=? AND bot_id=?",
                                       (str(latest_grid_id), bot_id)
                                   ).fetchone()
                                   actual_fill_price = float(_fb_row[0]) if _fb_row and _fb_row[0] else float(current_price)
                               except Exception: actual_fill_price = float(current_price)
                           logger.info(f"✅ {name}: Stored GRID ID {latest_grid_id} is FILLED @ {actual_fill_price} (Qty: {actual_fill_qty}). Processing INLINE.")
                           # 🚀 CRITICAL FIX: Process the fill inline — do NOT delegate to offline sync.
                           # The periodic reconciler does not call reconstruct_offline_fills frequently enough;
                           # delegating caused an infinite "Blocked by Local DB Lock" loop.
                           if actual_fill_qty <= 0 or actual_fill_price <= 0:
                               logger.error(f"❌ {name}: Cannot process inline fill — zero qty={actual_fill_qty} or price={actual_fill_price}. Evicting grid to unblock.")
                           else:
                               try:
                                   # v2.0: credit_fill (idempotent) -> seal_trade_state (single writer)
                                   from engine.ledger import credit_fill as _cf, seal_trade_state as _sts
                                   from engine.database import update_order_status as _uos
                                   _credited = _cf(
                                       bot_id=bot_id,
                                       order_id=str(latest_grid_id),
                                       cumulative_qty=actual_fill_qty,
                                       avg_price=actual_fill_price,
                                       order_type='grid',
                                       is_cumulative=True
                                   )
                                   _uos(latest_grid_id, 'filled', bot_id=bot_id, filled_qty=actual_fill_qty)
                                   if _credited:
                                       _sts(bot_id)
                                   logger.info(
                                       f"[INLINE-GRID-FILL] {name}: credit_fill->seal complete. "
                                       f"qty={actual_fill_qty} @ {actual_fill_price}. Lock cleared."
                                   )
                               except Exception as _fill_err:
                                   logger.error(f"[INLINE-GRID-FILL] {name}: Failed {latest_grid_id}: {_fill_err}")
                           local_grid_ids = []  # Clear lock so the next step's grid can be placed
                      elif status_str in ['new', 'open', 'partially_filled']:
                           # 🚀 SNAPSHOT LAG FIX: Order is CONFIRMED LIVE on exchange but absent from
                           # the stale start-of-cycle snapshot. Back-populate WS cache and do NOT evict.
                           logger.info(f"✅ {name}: Stored Grid {latest_grid_id} is CONFIRMED LIVE (status={status_str}) — snapshot was stale. Healing cache.")
                           from engine.ws_cache import get_ws_cache as _gwsc
                           _gwsc().update_order(str(latest_grid_id), order_status)
                           # Keep local_grid_ids intact — the lock correctly reflects exchange reality
                      elif status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                          logger.info(f"🚫 {name}: Stored GRID ID {latest_grid_id} is CANCELLED on exchange. Evicting from DB state.")
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id)
                          local_grid_ids = []  # Clear locals to unblock
                      else:
                          logger.warning(f"⏳ {name}: Stored Grid {latest_grid_id} status is {status_str}, but missing from open_orders! Forcing CANCEL and Eviction.")
                          try:
                              exchange.cancel_order(latest_grid_id, pair)
                          except: pass
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id)
                          local_grid_ids = []  # Clear to allow grid logic below to immediately fire
                  except Exception as _evict_err:
                      err_str = str(_evict_err).lower()
                      if "not found" in err_str or "-2013" in err_str:
                          logger.warning(f"🚫 {name}: Stored GRID ID {local_grid_ids[-1]} NOT FOUND on exchange. Evicting from DB state.")
                          from engine.database import update_order_status as _uos
                          _uos(local_grid_ids[-1], 'cancelled', bot_id=bot_id)
                          local_grid_ids = []
                      else:
                          logger.error(f"❌ {name}: Failed to evict stalemate GRID ID: {_evict_err}")
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
             
             # 🚀 STRICT ATR GUARD: If grid_price is 0, the strategy aborted.
             if grid_price <= 0:
                 logger.warning(f"🛑 {name}: Strategy returned INVALID grid price (0.0). Aborting placement to prevent drift. Reason: {grid_explain}")
                 update_bot_error(bot_id, f"Grid Error: {grid_explain}")
                 return None

             # 🚀 SPREAD-CROSSING MAKER LOOP FIX (Root Cause of Flashing)
             # Same as TP logic: If a Post-Only Grid crosses the active spread, Binance silently EXPIRES it.
             # Standard Grid: LONG bot buys to open line. Must be <= Best Bid.
             #                SHORT bot sells to open line. Must be >= Best Ask.
             try:
                 bid, ask = exchange.get_best_bid_ask(pair)
                 if bid is not None and ask is not None:
                     bid_val = float(bid)
                     ask_val = float(ask)
                     if direction == 'LONG':
                         # We are Buying to open. If Grid is higher than or equals Ask, it's a Taker.
                         if grid_price >= ask_val:
                             old_px = grid_price
                             grid_price = bid_val
                             logger.info(f"🚀 {name}: Grid Spread Cross Prevented! (Buy {old_px} >= Ask {ask_val}). Adjusted to Bid {grid_price} to preserve GTX.")
                     else:
                         # We are Selling to open. If Grid is lower than or equals Bid, it's a Taker.
                         if grid_price <= bid_val:
                             old_px = grid_price
                             grid_price = ask_val
                             logger.info(f"🚀 {name}: Grid Spread Cross Prevented! (Sell {old_px} <= Bid {bid_val}). Adjusted to Ask {grid_price} to preserve GTX.")
             except Exception as e:
                 logger.warning(f"⚠️ {name}: Market Gap check failed ({e}). Proceeding without spread-cross protection.")
                     
             logger.info(f"🔍 [GRID-MAINTENANCE] {name}: Target=${grid_price} | {grid_explain}")

             if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains Grid for {pair} @ {grid_price}")
                else:
                    logger.info(f"🔍 [GRID-DEBUG] Bot {name} ({direction}) | Price={current_price} | GridTarget={grid_price} | Amount={grid_amount} | Step={bot_status['current_step']} | BaseSize={bot_config.get('base_size')} | Multi={bot_config.get('martingale_multiplier')} | StratBase={strategy.params.get('base_size')} | StratMult={strategy.params.get('martingale_multiplier')}")
                    
                    side = 'buy' if direction == 'LONG' else 'sell'
                    
                    # 🚀 FAT FINGER GUARD: Dynamic Max-Size Protocol
                    try:
                        base_size_usd = float(strategy.params.get('base_size', 150.0))
                        multiplier = float(strategy.params.get('martingale_multiplier', 2.0))
                        max_step = int(strategy.max_steps)
                        abs_max_usd = base_size_usd * (multiplier ** max_step) * 1.5
                        abs_max_qty = abs_max_usd / current_price
                        
                        if grid_amount > abs_max_qty:
                            logger.critical(f"🛑 FAT FINGER BLOCK: {name} Grid Amount {grid_amount} drastically exceeds strategy bounds ({abs_max_qty:.4f} max limit). Cancelling Grid placement.")
                            update_bot_error(bot_id, "FAT FINGER GUARD: Grid size exceeds strategy absolutes.")
                            return None
                    except: pass
                    
                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        # v2.0: Distinguish between qty-too-small (config problem) vs other rejections
                        if 'min' in msg.lower() or 'notional' in msg.lower() or 'qty' in msg.lower() or 'size' in msg.lower():
                            logger.warning(
                                f"[GRID-QTY-GUARD] {name}: Grid qty too small for exchange minimum. "
                                f"msg='{msg}' | grid_amount={grid_amount:.6f} @ {grid_price:.4f} "
                                f"(notional=${grid_amount * grid_price:.2f}). "
                                f"Increase base_size in bot config to resolve."
                            )
                            update_bot_error(bot_id, f"Grid Qty too small for exchange. Base Size too low?")
                        else:
                            logger.error(f"[GRID-VAL-FAIL] {name}: Grid validation failed: {msg}")
                            update_bot_error(bot_id, f"Grid Validation Failed: {msg}")
                    else:
                        try:
                            cycle_id = bot_status.get('cycle_id', 0)
                            client_order_id_grid = self._generate_deterministic_id(bot_id, 'GRID', cycle_id, bot_status['current_step'] + 1)

                            # v2.0: Guard against placing a grid when we already have one from
                            # a recent retry attempt (_R or _F suffix from GTX retry logic).
                            # The retry itself already placed the order — don't double-place.
                            retry_cids = {client_order_id_grid + '_R', client_order_id_grid + '_F'}
                            already_placed = [o for o in valid_grid_orders
                                              if o.get('clientOrderId', '') in retry_cids]
                            if already_placed:
                                logger.info(
                                    f"[GRID-DEDUP] {name}: Retry-suffix grid already live "
                                    f"({already_placed[0].get('clientOrderId')}). Skipping fresh placement."
                                )
                                return None

                            ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'postOnly': True, 'timeInForce': 'GTX'}
                            
                            # 🚀 SPREAD-CROSS FIX: If the grid target evaluates to precisely the current active market price
                            # (due to rapid drops/gaps), drop Maker-Only GTX flag to prevent immediate rejection (-2010).
                            if grid_price == exchange.round_to_step(current_price, exchange.get_symbol_precision(pair)['tick_size']):
                                logger.warning(f"⚠️ {name}: Grid price matches active market gap. Dropping GTX Maker flag to allow execution.")
                                ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'timeInForce': 'GTC'}
                                
                            order = self._place_gtx_order_with_retry(exchange, pair, side, grid_amount, grid_price, params=ccxt_grid_params, label=f"{name}-MAINTAIN-GRID", position_side=direction)
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
                                update_bot_error(bot_id, f"Exchange Error: {e}")
                            
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
            pass # conn.close() disabled for singleton safety

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
            # 🚀 FIX: get_precision() was renamed to get_symbol_precision() — use the correct method
            prec_data = exchange.get_symbol_precision(pair) or {}
            price_prec = int(prec_data.get('price_precision', 2))
            qty_prec = int(prec_data.get('qty_precision', 3))
            step_size = float(prec_data.get('step_size', 0.001))

            # 🚀 UNIVERSAL PRECISION: Use strategy's Decimal Guardian for all rounding
            delta_qty = strat._round_qty(delta_qty)
            lock_price_r = strat._round_price(lock_price)

            if delta_qty <= 0 or lock_price_r <= 0:
                logger.error(f"🛡️ {name}: Invalid hedge delta params — qty={delta_qty}, price={lock_price_r}")
                return None

            logger.warning(
                f"🛡️ [HEDGE-LOCK] Bot {name} (step {trigger_step}): Placing {hedge_side.upper()} "
                f"{'DELTA ' if filled_qty > 0 else ''}limit GTX {delta_qty} @ {lock_price_r}"
            )

            # 4. Deterministic CID
            cycle_id = bot_status.get('cycle_id', 0)
            cid = self._generate_deterministic_id(bot_id, 'HEDGE', cycle_id, trigger_step)
            
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
            pass # conn.close() disabled for singleton safety

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

        # Close the position with a market order safely
        try:
            position_side = 'sell' if direction == 'LONG' else 'buy'
            
            if bot_status['avg_entry_price'] > 0:
                size_to_close = bot_status['total_invested'] / bot_status['avg_entry_price']
                actual_size = abs(size_to_close)

                # 🚀 SYSTEM DISCREPANCY GUARD (GHOST WIPE)
                # Evaluate aggregate DB vs Aggregate Physical
                phys_positions = exchange.fetch_positions()
                phys_long = 0.0
                phys_short = 0.0
                for p in (phys_positions or []):
                    if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                        size = float(p.get('contracts', 0) or abs(float(p.get('positionAmt', 0))))
                        pt_side = p.get('side', '').upper()
                        if not pt_side: 
                            pos_amount = float(p.get('positionAmt', 0))
                            if pos_amount < 0: pt_side = 'SHORT'
                            elif pos_amount > 0: pt_side = 'LONG'
                        if pt_side == 'SHORT': phys_short += size
                        elif pt_side == 'LONG': phys_long += size
                phys_net_qty = phys_long - phys_short
                
                from engine.database import get_connection as _st_conn
                sib_net_qty = 0.0
                with _st_conn() as _c:
                    _cur = _c.cursor()
                    _cur.execute(
                        "SELECT direction, total_invested, avg_entry_price FROM trades "
                        "JOIN bots ON trades.bot_id = bots.id WHERE bots.pair = ? AND trades.total_invested > 0", 
                        (pair,)
                    )
                    for sib_dir, s_inv, s_avg in _cur.fetchall():
                        if float(s_avg) > 0:
                            s_qty = float(s_inv) / float(s_avg)
                            if str(sib_dir).upper() == 'LONG': sib_net_qty += s_qty
                            else: sib_net_qty -= s_qty

                divergence_qty = abs(sib_net_qty - phys_net_qty)
                divergence_usd = divergence_qty * current_price
                
                if divergence_usd > 10.0:
                    logger.critical(f"🛑 {name}: SL/Market Close Blocked! System net vs physical diverges by ${divergence_usd:.2f}. Bypassing API to strictly wipe Ghost DB.")
                    from engine.database import safe_wipe_bot
                    safe_wipe_bot(bot_id, pair, direction, reason="SL_GHOST_WIPE: divergence > $10", exit_price=current_price)
                    return

                if actual_size > 0:
                    logger.warning(f"Placing market order to close {actual_size} {pair} {position_side} for bot {name} SL")
                    order = None
                    try:
                        order = exchange.create_order(pair, 'market', position_side, actual_size)
                    except Exception as e_order:
                        logger.error(f"❌ {name}: Failed to place SL Market Order ({e_order}). Purging local Ghost state.")
                        from engine.database import safe_wipe_bot
                        safe_wipe_bot(bot_id, pair, direction, reason=f"SL_API_REJECT_GHOST", exit_price=current_price)
                        return

                    if order:
                        from engine.database import reset_bot_after_tp
                        log_trade(bot_id, 'STOP_LOSS_EXIT', pair, current_price, actual_size, current_price * actual_size, f'SL_MARKET_{bot_id}', bot_status['current_step'], "SL Market Exit", (current_price - bot_status['avg_entry_price']) * actual_size)
                        reset_bot_after_tp(bot_id, current_price, direction=direction, action_label='STOP_LOSS_EXIT')
                        logger.info(f"✅ {name}: Market order placed to close SL for {pair} (ID: {order['id']})")
                else:
                    logger.info(f"ℹ️ {name}: No virtual size to close. Running wipe guard before DB reset.")
                    from engine.database import safe_wipe_bot
                    safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_NO_VIRTUAL_POSITION", exit_price=current_price)
            else:
                logger.info(f"ℹ️ {name}: Bot has 0 avg_entry_price. Running wipe guard before DB reset.")
                from engine.database import safe_wipe_bot
                safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_ZERO_PRICE", exit_price=current_price)

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
