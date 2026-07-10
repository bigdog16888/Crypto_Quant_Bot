import logging

import time

import sqlite3

from typing import Dict, List, Optional, Tuple, Any, Set

from dataclasses import dataclass

from enum import Enum

from datetime import datetime



from .database import (

    get_connection, get_bot_status, get_all_bots, reset_bot_after_tp,

    log_trade, get_bot_order_ids, save_bot_order, update_order_status,

    update_martingale_step, log_reconciliation, safe_wipe_bot,

    get_manual_whitelists, clear_manual_whitelists_for_pair,

    DB_PATH, _reset_bot_after_tp_internal

)

from .exchange_interface import ExchangeInterface, normalize_symbol

from config.settings import config



logger = logging.getLogger("StateReconciliation")



ENGINE_START_TIME = int(time.time())

def flag_bot_manual_proof(bot_id: int, reason: str):
    from engine.database import get_connection
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=?",
            (bot_id,),
        )
        conn.commit()
        logger.warning(f"Bot {bot_id} flagged as REQUIRE_MANUAL_PROOF: {reason}")
    except Exception as e:
        logger.error(f"Failed to flag bot {bot_id} as REQUIRE_MANUAL_PROOF: {e}")




# 🧬 Helpers for internal symbol normalization and Testnet/Mainnet positionSide

def _nsym(pair: str) -> str:

    """Normalize exchange symbols to match internal bot ticker format."""

    if not pair: return ""

    return pair.split(':')[0].replace('/', '').upper()



def _safe_order_params(params: dict, is_testnet: bool = False) -> dict:

    """

    Strips or normalizes positionSide for One-Way mode compatibility.

    Testnet requires positionSide='BOTH'. Mainnet rejects positionSide entirely.

    Call this on every params dict before create_order() in the reconciler.

    """

    result = dict(params) if params else {}

    current = str(result.get('positionSide', '')).upper()

    if current in ('LONG', 'SHORT', ''):

        result.pop('positionSide', None)

    if is_testnet:

        result['positionSide'] = 'BOTH'

    return result



def _to_ccxt_pair(normalized_or_ccxt: str, all_bot_pairs: list = None) -> str:

    """

    Given either a normalized symbol ('SOLUSDC') or a CCXT symbol

    ('SOL/USDC:USDC'), returns the CCXT format suitable for exchange calls.



    Strategy:

    1. If input already contains '/' or ':', return as-is (already CCXT).

    2. If all_bot_pairs provided, scan for a DB pair whose normalized form

       matches the input — return that DB pair (canonical CCXT form).

    3. Fallback: return input unchanged (caller must handle miss).

    """

    s = str(normalized_or_ccxt or '').strip()

    if '/' in s or ':' in s:

        return s  # Already CCXT format

    if all_bot_pairs:

        def _norm(x):

            return str(x).replace('/', '').replace('-', '').split(':')[0].upper()

        for p in all_bot_pairs:

            candidate = p[0] if isinstance(p, (list, tuple)) else p

            if _norm(candidate) == s.upper():

                return candidate

    return s  # Fallback: return as-is





class ReconciliationAction(Enum):

    """Actions to take during reconciliation"""

    NO_ACTION = "no_action"

    RESET_TO_IDLE = "reset_to_idle"

    MARK_TP_HIT = "mark_tp_hit"

    REPAIR_ORDERS = "repair_orders"

    REQUIRE_MANUAL = "require_manual"

    SYSTEM_FIX_ZOMBIE = "system_fix_zombie"

    SYSTEM_FIX_ORPHAN = "system_fix_orphan"

    ALERT_ONLY = "alert_only"

    ROGUE_POSITION = "rogue_position"





@dataclass

class BotState:

    """Represents a bot's current state"""

    bot_id: int

    name: str

    pair: str

    direction: str

    is_active: bool

    # Trade state

    in_trade: bool

    total_invested: float

    avg_entry_price: float

    target_tp_price: float

    current_step: int

    basket_start_time: int

    # Order tracking

    entry_order_id: Optional[str]

    tp_order_id: Optional[str]

    has_confirmed_entry: bool

    cycle_id: int = 1  # Current trading cycle. Increments on each TP reset.

    cycle_start_time: int = 0  # 🚀 authoritative cycle boundary (Absolute Age)

    # Bot configuration for mathematical step recovery

    base_size: float = 0.0          # First-step notional (e.g. $100)

    martingale_multiplier: float = 1.0  # Each step size = prev_step * multiplier

    bot_status: str = ''  # Raw DB status string (e.g. 'IN TRADE', 'REQUIRE_MANUAL_PROOF', 'Scanning')

    cycle_phase: str = 'IDLE'

    parent_bot_id: Optional[int] = None

    hedge_child_bot_id: Optional[int] = None





@dataclass

class ExchangePosition:

    """Represents position data from exchange"""

    symbol: str

    side: str  # 'LONG' or 'SHORT'

    size: float

    entry_price: float

    mark_price: float

    unrealized_pnl: float





@dataclass

class ExchangeOrder:

    """Represents order data from exchange"""

    order_id: str

    symbol: str

    side: str  # 'buy', 'sell'

    order_type: str  # 'limit', 'market'

    price: float

    amount: float

    status: str  # 'open', 'filled', 'cancelled'

    client_order_id: Optional[str]





@dataclass

class ReconciliationResult:

    """Result of reconciliation for a bot"""

    bot_id: int

    bot_name: str

    pair: str

    action_taken: ReconciliationAction

    details: str

    requires_manual_intervention: bool = False





class StateReconciler:

    """

    Refactored State Reconciler (v2.0)

    

    Philosophy:

    1. Bot-Centric: The Bot DB is the source of truth for "Intent".

    2. Verification: The Exchange is the source of truth for "Reality".

    3. Net-Sum: We validate the Aggregate Virtual Position against the Net Physical Position.

    """

    

    def __init__(self, exchanges: Optional[Dict[str, ExchangeInterface]] = None):

        if exchanges is not None:

            self.exchanges = exchanges

        else:

            if getattr(config, 'FUTURES_ONLY_MODE', False):

                self.exchanges = {

                    'future': ExchangeInterface(market_type='future')

                }

            else:

                self.exchanges = {

                    'spot': ExchangeInterface(market_type='spot'),

                    'future': ExchangeInterface(market_type='future')

                }

        self.results: List[ReconciliationResult] = []

        self.cid_cache: Dict[str, str] = {}  # Cache order_id -> client_order_id to prevent API spam on orphans

        # 📸 SINGLE SNAPSHOT ARCHITECTURE: fetched once at startup, passed to all reconciliation passes.

        # Prevents the 3-API-call race condition that caused ghost detections on cold start.

        self._startup_snapshot: Dict[str, list] = {}  # {market_type: [positions]}
        self._flat_snapshots_counts: Dict[str, int] = {}

        

    def get_exchange(self, market_type: str):

        if market_type in self.exchanges:

            return self.exchanges[market_type]

        if not self.exchanges:

            return None

        return list(self.exchanges.values())[0]

    def _audit_pending_exits(self) -> None:
        """
        Continuous Fill Audit:
        Runs every cycle to check for active bots with TP orders that filled on the exchange
        but were missed by the WebSocket.
        """
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT b.id, b.pair, b.name, t.tp_order_id, t.open_qty, b.config, t.cycle_id
                FROM bots b
                JOIN trades t ON b.id = t.bot_id
                WHERE t.tp_order_id IS NOT NULL 
                  AND t.open_qty > 0 
                  AND t.entry_confirmed = 1
                  AND b.is_active = 1
            """)
            rows = cursor.fetchall()
            if not rows:
                return

            for row in rows:
                bot_id, pair, name, tp_order_id, open_qty, config_json, cycle_id = row
                if not tp_order_id:
                    continue

                # Check in local DB if this order already has a filled status
                order_row = cursor.execute(
                    "SELECT status, filled_amount FROM bot_orders WHERE (order_id = ? OR client_order_id = ?) AND bot_id = ?",
                    (tp_order_id, tp_order_id, bot_id)
                ).fetchone()

                if order_row and order_row[0] in ('filled', 'cancelling'):
                    continue

                try:
                    import json
                    from config.settings import config as app_config
                    from engine.exchange_interface import normalize_market_type
                    
                    cfg = json.loads(config_json) if config_json else {}
                    mt = normalize_market_type(cfg.get('market_type', app_config.MARKET_TYPE))
                    exchange = self.exchanges.get(mt)
                    if not exchange:
                        logger.warning(f"[_audit_pending_exits] No exchange instance for market_type '{mt}' (bot={bot_id})")
                        continue

                    logger.info(f"🔎 [_audit_pending_exits] Fetching TP {tp_order_id} status via REST for Bot {bot_id} ({pair})...")
                    if tp_order_id and any(str(tp_order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                        logger.info(f"🔎 [_audit_pending_exits] Skipping fetch_order for synthetic TP order {tp_order_id}")
                        continue
                    order = exchange.fetch_order(tp_order_id, pair)
                    if not order:
                        logger.warning(f"[_audit_pending_exits] Fetch order returned None for order {tp_order_id} bot {bot_id}")
                        continue

                    # If exchange returns status='filled'
                    if order.get('status') == 'filled':
                        fill_price = float(order.get('average') or order.get('price') or 0)
                        fill_qty = float(order.get('filled') or 0)
                        fill_ts_ms = order.get('lastTradeTimestamp') or order.get('timestamp') or 0
                        fill_ts = int(fill_ts_ms // 1000) if fill_ts_ms else int(time.time())

                        if fill_qty <= 0 or fill_price <= 0:
                            logger.warning(f"[_audit_pending_exits] Order {tp_order_id} filled but filled_qty/avg_price invalid: qty={fill_qty}, price={fill_price}")
                            continue

                        from engine.ledger import credit_fill, seal_trade_state, register_tp_cascade
                        
                        logger.info(
                            f"🚨 [FILL-AUDIT-RECOVERY] Bot {bot_id}: TP {tp_order_id} confirmed filled on exchange, "
                            f"crediting missed fill (qty={fill_qty} @ price={fill_price})."
                        )
                        
                        credited = credit_fill(
                            bot_id=bot_id,
                            order_id=tp_order_id,
                            cumulative_qty=fill_qty,
                            avg_price=fill_price,
                            order_type='tp',
                            is_cumulative=True,
                            fill_ts=fill_ts
                        )
                        
                        if credited:
                            # Register cascade to trigger runner's cancel-and-reset flow
                            register_tp_cascade(bot_id, pair, fill_price, exit_fill_ts=fill_ts)
                            # After crediting, call seal_trade_state(bot_id)
                            seal_trade_state(bot_id)
                        else:
                            logger.warning(
                                f"⚠️ [FILL-AUDIT-RECOVERY] credit_fill returned False for TP {tp_order_id} bot {bot_id}. "
                                f"No matching row found in bot_orders."
                            )
                except Exception as ex_err:
                    logger.error(f"[_audit_pending_exits] Failed checking order {tp_order_id} for bot {bot_id}: {ex_err}")

        except Exception as err:
            logger.error(f"Error in continuous fill audit: {err}")

    def _audit_pending_grids(self) -> None:
        """
        Continuous Grid Audit:
        Checks for active bots with pending/new grid or entry orders that filled on the exchange
        but were missed by the WebSocket. Caps REST calls at 20 per run.
        """
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT b.id, b.pair, b.name, bo.order_id, bo.client_order_id, bo.order_type, b.config, bo.created_at
                FROM bots b
                JOIN trades t ON b.id = t.bot_id
                JOIN bot_orders bo ON b.id = bo.bot_id
                WHERE bo.status IN ('new', 'open', 'partially_filled', 'placing', 'cancelling')
                  AND bo.order_type IN ('grid', 'entry')
                  AND bo.cycle_id = t.cycle_id
                  AND b.is_active = 1
                  AND bo.order_id IS NOT NULL
                  AND bo.order_id != ''
                  AND bo.order_id NOT LIKE 'PENDING_%'
                ORDER BY bo.created_at ASC
            """)
            rows = cursor.fetchall()
            if not rows:
                return

            now = int(time.time())
            older_orders = [r for r in rows if now - (r[7] or 0) > 60]
            newer_orders = [r for r in rows if now - (r[7] or 0) <= 60]
            qualifying_rows = older_orders + newer_orders

            if len(qualifying_rows) > 20:
                logger.warning(f"⚠️ [GRID-AUDIT-CAP] Truncated grid fill audit. Processing oldest 20 of {len(qualifying_rows)} orders.")
                qualifying_rows = qualifying_rows[:20]

            for row in qualifying_rows:
                bot_id, pair, name, order_id, client_order_id, order_type, config_json, created_at = row
                try:
                    import json
                    from config.settings import config as app_config
                    from engine.exchange_interface import normalize_market_type
                    
                    cfg = json.loads(config_json) if config_json else {}
                    mt = normalize_market_type(cfg.get('market_type', app_config.MARKET_TYPE))
                    exchange = self.exchanges.get(mt)
                    if not exchange:
                        logger.warning(f"[_audit_pending_grids] No exchange instance for market_type '{mt}' (bot={bot_id})")
                        continue

                    logger.info(f"🔎 [_audit_pending_grids] Fetching grid/entry {order_id} status via REST for Bot {bot_id} ({pair})...")
                    if order_id and any(str(order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                        continue

                    order = exchange.fetch_order(order_id, pair)
                    if not order:
                        logger.warning(f"[_audit_pending_grids] Fetch order returned None for order {order_id} bot {bot_id}")
                        continue

                    if order.get('status') == 'filled':
                        fill_price = float(order.get('average') or order.get('price') or 0)
                        fill_qty = float(order.get('filled') or 0)
                        fill_ts_ms = order.get('lastTradeTimestamp') or order.get('timestamp') or 0
                        fill_ts = int(fill_ts_ms // 1000) if fill_ts_ms else int(time.time())

                        if fill_qty <= 0 or fill_price <= 0:
                            logger.warning(f"[_audit_pending_grids] Order {order_id} filled but filled_qty/avg_price invalid: qty={fill_qty}, price={fill_price}")
                            continue

                        from engine.parity_gates import get_exchange_signed_net
                        from engine.database import get_pair_virtual_net

                        # After confirming order filled, verify exchange net position actually increased
                        current_exchange_net = get_exchange_signed_net(exchange, pair)
                        if current_exchange_net is not None:
                            current_virtual_net = get_pair_virtual_net(pair)
                            actual_delta = current_exchange_net - current_virtual_net

                            if abs(actual_delta) < fill_qty * 0.5:  # less than 50% of expected increase
                                logger.warning(
                                    f"[GRID-AUDIT-SKIP] Bot {bot_id}: order {order_id} shows filled={fill_qty} "
                                    f"but exchange net delta={actual_delta:.6f} doesn't support crediting. "
                                    f"Possible hedge offset. Skipping credit to prevent phantom fill."
                                )
                                continue

                        from engine.ledger import credit_fill, seal_trade_state
                        
                        logger.info(
                            f"🚨 [GRID-AUDIT-RECOVERY] Bot {bot_id}: Grid/Entry {order_id} confirmed filled on exchange, "
                            f"crediting missed fill (qty={fill_qty} @ price={fill_price})."
                        )
                        
                        credited = credit_fill(
                            bot_id=bot_id,
                            order_id=order_id,
                            cumulative_qty=fill_qty,
                            avg_price=fill_price,
                            order_type=order_type.lower(),
                            is_cumulative=True,
                            fill_ts=fill_ts
                        )
                        if credited:
                            seal_trade_state(bot_id)
                    elif order.get('status') in ('canceled', 'cancelled', 'expired', 'rejected'):
                        from engine.database import update_order_status
                        logger.info(f"🚨 [GRID-AUDIT-RECOVERY] Bot {bot_id}: Grid/Entry {order_id} confirmed canceled on exchange. Updating DB.")
                        update_order_status(order_id, 'cancelled', bot_id, 0.0)
                except Exception as ex_err:
                    logger.error(f"[_audit_pending_grids] Failed checking order {order_id} for bot {bot_id}: {ex_err}")
        except Exception as err:
            logger.error(f"Error in continuous grid fill audit: {err}")




    def prime_startup_snapshot(self) -> Dict[str, list]:

        """

        📸 SINGLE STARTUP SNAPSHOT — Phase 2 Architecture.



        Fetches exchange positions ONCE across all market types, writes the DB

        snapshot atomically, and stores in self._startup_snapshot.



        ALL subsequent startup reconciliation passes (reconstruct_offline_fills,

        adopt_from_physical_positions, _align_memory_to_ledger) consume this

        snapshot instead of making independent API calls.



        Call this immediately after StateReconciler is constructed at startup.

        Never call it from within run_cycle — the cycle already has its own snapshot.

        """

        from engine.database import update_active_positions_snapshot

        all_positions = []

        self._startup_snapshot = {}



        for mt, ex in self.exchanges.items():

            if not ex:

                continue

            try:

                positions = ex.fetch_positions()

                if positions is None:

                    logger.warning(f"[SNAPSHOT] fetch_positions returned None for {mt} — skipping.")

                    continue

                self._startup_snapshot[mt] = positions

                all_positions.extend(positions)

                logger.info(f"[SNAPSHOT] {mt}: {len(positions)} positions fetched.")

            except Exception as e:

                logger.error(f"[SNAPSHOT] Failed to fetch positions for {mt}: {e}")



        # Atomic DB write — single source of truth for the UI immediately at startup

        try:

            update_active_positions_snapshot(all_positions)

            logger.info(f"✅ [SNAPSHOT] Active positions table updated atomically ({len(all_positions)} total).")

        except Exception as e:

            logger.error(f"[SNAPSHOT] Failed to write DB snapshot: {e}")



        return self._startup_snapshot





    # ------------------------------------------------------------------

    # STEP 1: OFFLINE FILL DETECTION

    # ------------------------------------------------------------------

    def _sync_positions_to_exchange(self, exchange: ExchangeInterface) -> None:

        """

        DISABLED: The user enforced strict proof-of-order-ID consensus.

        Blindly adopting physical exchange positions without a corresponding

        `clientOrderId` receipt (DNA) constitutes a mathematical hallucination.

        All gap reconciliation is now securely routed exclusively through

        `reconstruct_offline_fills` which strictly requires order ID proof.

        Unproven anomalies will drop down to MANUAL REQUIREMENT.

        """

        pass



    def reconstruct_offline_fills(self, since_hours: int = 6, pair_filter: Optional[str] = None, forensic_mode: bool = False) -> Dict[str, int]:
        from engine.write_queue import WriteQueue
        return WriteQueue().put_and_wait(self._reconstruct_offline_fills_internal, since_hours, pair_filter, forensic_mode, _wq_timeout=120.0)

    def _reconstruct_offline_fills_internal(self, since_hours: int = 6, pair_filter: Optional[str] = None, forensic_mode: bool = False) -> Dict[str, int]:

        from engine.parity_gates import forensic_adopt_allowed
        if forensic_mode and not forensic_adopt_allowed():
            logger.warning(
                "[FORENSIC-DISABLED] forensic_mode=True but ALLOW_FORENSIC_ADOPT=False — "
                "skipping anonymous fill attribution."
            )
            forensic_mode = False

        """

        Scans exchange history for orders that filled while we were offline.

        Updates the DB immediately so subsequent checks see the correct state.

        Refactored to be ROBUST: Uses Client Order ID parsing to reconstruct state.

        

        Args:

            since_hours: How far back to search (default 7 days).

            pair_filter: If set (normalized symbol e.g. 'BTCUSDC'), only scans that

                         specific pair. Bypasses the 15-min global cooldown because

                         this is a targeted surgical scan, not a full sweep.

        """

        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total': 0}

        

        # 🛡️ GLOBAL COOLDOWN (15 minutes) to prevent API spam during persistent gaps.

        # BYPASS: pair_filter scans are surgical (one pair only) — no spam risk.

        current_time = time.time()

        if pair_filter is None:

            last_scan = getattr(StateReconciler, '_last_global_offline_scan', 0.0)

            if current_time - last_scan < 900:

                logger.debug(f"⏳ [FILL-SCAN] Skipping offline fill scan (on 15m cooldown, {int(900 - (current_time - last_scan))}s left).")

                return stats

            StateReconciler._last_global_offline_scan = current_time

        else:

            # Per-pair cooldown: 3 minutes per pair to avoid re-scanning on every cycle

            _pair_key = f'_last_pair_scan_{pair_filter}'

            last_pair_scan = getattr(StateReconciler, _pair_key, 0.0)

            if current_time - last_pair_scan < 180:

                logger.debug(f"⏳ [FILL-SCAN] Skipping targeted {pair_filter} scan (3m cooldown, {int(180 - (current_time - last_pair_scan))}s left).")

                return stats

            setattr(StateReconciler, _pair_key, current_time)

            logger.info(f"🔎 [FILL-SCAN] Targeted pair scan triggered for {pair_filter} (bypassing global cooldown).")

        

        # 🔑 PREFLIGHT: Sync DB positions to exchange before looking at any single fill

        from engine.database import get_connection as _gc

        _all_pairs = [r[0] for r in _gc().execute("SELECT DISTINCT pair FROM bots").fetchall()]

        

        # This prevents fragmented/slow fills from corrupting a position that already matches

        try:

            ex_fut = self.exchanges.get('future')

            if ex_fut:

                self._sync_positions_to_exchange(ex_fut)

        except Exception as e:

            logger.error(f"Preflight sync failed: {e}")



        # 1. Identify pairs to scan (Active Bots + Open Orders)

        conn = get_connection()

        cursor = conn.cursor()

        

        # Get all bots to know what pairs to check

        cursor.execute("SELECT id, pair, name, status FROM bots WHERE is_active=1")

        active_bots = cursor.fetchall() # [(id, pair, name, status), ...]

        

        # Get pairs from open orders too (CCXT maps some open states to 'new' or 'open')

        cursor.execute("SELECT DISTINCT pair from bots WHERE id IN (SELECT DISTINCT bot_id FROM bot_orders WHERE status IN ('open', 'new', 'placing', 'cancelling'))")

        order_pairs = [r[0] for r in cursor.fetchall()]

        

        # We will restrict this later using absolute mathematical gap verification.

        # pairs_to_check = set([b[1] for b in active_bots] + order_pairs)

        

        # Pre-fetch Bot States for fast lookups

        # Map: bot_id -> {current_step, total_invested, avg_entry, basket_start_time,

        #                  cycle_id, wipe_wall_ts, cycle_start_time}

        # cycle_start_time (v2.1.0): authoritative exchange-event-anchored cycle boundary.

        # basket_start_time: kept for EE timer (engine-operation timestamp, NOT cycle boundary).

        bot_states = {}

        cursor.execute(

            "SELECT bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, "

            "basket_start_time, COALESCE(cycle_id, 1), COALESCE(wipe_wall_ts, 0), "

            "COALESCE(cycle_start_time, 0) FROM trades"

        )

        for row in cursor.fetchall():

            bot_states[row[0]] = {

                'current_step': row[1], 'total_invested': row[2], 

                'avg_entry': row[3], 'entry_confirmed': row[4],

                'basket_start_time': row[5] or 0,

                'cycle_id': row[6],

                'wipe_wall_ts': row[7],

                'cycle_start_time': row[8] or 0,  # v2.1.0 authoritative boundary

            }



        pass # conn.close() disabled for singleton safety # Close mainly to keep scope clean, we'll reopen for updates



        # 🚀 SELF-HEALING: basket_start_time Recovery

        # If a bot is actively IN TRADE (total_invested > 0) but basket_start_time=0,

        # EE decay is permanently silent. Recover the timestamp from the oldest filled order

        # in this session — this runs every reconciler pass so no restart is ever needed.

        _heal_conn = get_connection()

        _heal_cur = _heal_conn.cursor()

        _healed = 0

        for _bot_id, _state in bot_states.items():

            if _state['total_invested'] > 0 and _state['basket_start_time'] == 0:

                # Try to recover from oldest filled entry order in bot_orders for the CURRENT cycle

                _heal_cur.execute("""

                    SELECT MIN(created_at) FROM bot_orders 

                    WHERE bot_id=? AND order_type='entry' AND status IN ('filled','closed') AND cycle_id=?

                """, (_bot_id, _state['cycle_id']))

                _row = _heal_cur.fetchone()

                recovered_ts = (_row[0] if _row and _row[0] else None)

                if not recovered_ts:

                    # Fallback: use current time so EE at least starts from now

                    recovered_ts = int(time.time())

                _heal_cur.execute(

                    "UPDATE trades SET basket_start_time=? WHERE bot_id=? AND basket_start_time=0",

                    (int(recovered_ts), _bot_id)

                )

                _state['basket_start_time'] = int(recovered_ts)  # update in-memory too

                _healed += 1

                logger.info(f"🩹 [SELF-HEAL] Bot {_bot_id}: basket_start_time recovered to {recovered_ts} (was 0 while in trade — EE now active).")

        if _healed:

            _heal_conn.commit()

        pass # _heal_conn.close() disabled for singleton safety





        # 1.5. 🚀 PRE-COMMIT ROW RESOLUTION

        # Covers two failure windows:

        # A) status='placing' — order written to DB BEFORE exchange API call; engine crashed mid-atomicity.

        # B) status='new', order_type='entry' — entry confirmed by exchange (API returned OK) but engine

        #    went offline BEFORE the WebSocket fill event arrived. This means filled_amount=0 in DB even

        #    though Binance fully executed the entry order.        # 🚀 DNA-GUARD v3.0: FULL-SPECTRUM ID RESOLUTION

        # We resolve ALL unresolved orders (placing, new, open) across ALL types.

        # This ensures the ledger is 100% accurate before parity checks run.

        _place_conn = get_connection()

        _place_cur = _place_conn.cursor()

        _place_cur.execute("""

            SELECT bo.id, bo.bot_id, b.pair, bo.order_type, bo.client_order_id,

                   bo.price, bo.amount, bo.step, bo.cycle_id

            FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id

            LEFT JOIN trades t ON bo.bot_id=t.bot_id

            WHERE (bo.status IN ('placing', 'new', 'open', 'cancelling'))

              AND bo.filled_amount < bo.amount

              AND bo.created_at >= COALESCE(t.wipe_wall_ts, 0)

        """)

        placing_rows = _place_cur.fetchall()



        if placing_rows:

            logger.info(f"🔍 [PRE-COMMIT-RESOLVE] Found {len(placing_rows)} unresolved 'placing' rows.")



        for p_row in placing_rows:

            db_id, bot_id, pair, otype, cid, price, amount, step, cycle_id = p_row

            for mt, ex in self.exchanges.items():

                if not ex: continue

                try:

                    exch_order = None
                    lookup_succeeded = False

                    # Try direct fetch by clientOrderId first (Binance supports this)

                    try:

                        if cid and any(str(cid).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                            logger.info(f"🔎 [PRE-COMMIT-RESOLVE] Skipping lookup for synthetic CID {cid}")
                            exch_order = None
                            lookup_succeeded = True
                        else:
                            exch_order = ex.fetch_order(cid, pair)
                            lookup_succeeded = True

                    except Exception as e_cid:
                        import ccxt
                        if isinstance(e_cid, (ccxt.OrderNotFound, ccxt.InvalidOrder)) or "OrderNotFound" in type(e_cid).__name__ or "order not found" in str(e_cid).lower() or "-2013" in str(e_cid):
                            lookup_succeeded = True

                    # Fallback: scan recent closed+open orders

                    if not exch_order and not (cid and any(str(cid).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_'))):

                        try:

                            # Prioritize open orders first, then closed orders to avoid matching stale/expired history
                            candids = (ex.fetch_open_orders(pair) or []) + (ex.fetch_closed_orders(pair, limit=50) or [])

                            matches = []

                            for o in candids:

                                if o.get('clientOrderId') == cid or (o.get('info') or {}).get('clientOrderId') == cid:

                                    matches.append(o)

                            if matches:

                                # Prefer active/open orders if they exist

                                active_matches = [o for o in matches if (o.get('status') or '').lower() in ('open', 'new', 'partially_filled', 'partiallyfilled')]

                                if active_matches:

                                    exch_order = max(active_matches, key=lambda x: (x.get('timestamp') or 0, int(x.get('id') or 0) if str(x.get('id', '')).isdigit() else 0))

                                else:

                                    exch_order = max(matches, key=lambda x: (x.get('timestamp') or 0, int(x.get('id') or 0) if str(x.get('id', '')).isdigit() else 0))

                        except Exception as _e:
                            logger.debug(f'[ORDER-LOOKUP] fetch_open/closed_orders failed (transient): {_e}')



                    if exch_order:

                        o_status = (exch_order.get('status') or '').lower()

                        o_id = exch_order.get('id')

                        o_filled = float(exch_order.get('filled', exch_order.get('filled_amount', 0.0)))

                        # Fallback if status is filled/closed but CCXT returned 0.0 filled amount
                        if o_status in ('filled', 'closed') and o_filled <= 0:
                            o_filled = float(exch_order.get('amount') or amount or 0.0)

                        o_price = float(exch_order.get('average', exch_order.get('price', price)))

                        if o_price <= 0: o_price = price # fallback



                        if o_status in ('filled', 'closed', 'open', 'new', 'partially_filled') or o_filled > 1e-8:

                            # 🚀 Fix 5: If there is a fill (even if status=canceled/expired), it's valid.

                            new_status = 'open' if o_status in ('open','new','partially_filled','partiallyfilled') else 'filled'

                            _place_cur.execute("""

                                UPDATE bot_orders 

                                SET order_id=?, status=?, filled_amount=?, price=?, updated_at=? 

                                WHERE id=?

                            """, (o_id, new_status, o_filled, o_price, int(time.time()), db_id))

                            logger.info(f"✅ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → found as {o_status} (id={o_id}, filled={o_filled}). Restored.")

                            

                            # 🚀 ATOMIC SYNC: If it's filled/closed (or canceled with fill), trigger ledger sync

                            if new_status == 'filled' or o_filled > 1e-8:

                                from .ledger import credit_fill, seal_trade_state

                                credit_fill(bot_id=bot_id, order_id=o_id, cumulative_qty=o_filled, avg_price=o_price, order_type=otype, is_cumulative=True, caller='reconciler')

                                # BUG 3 FIX: Unconditionally promote the DB row status to match exchange truth.
                                # credit_fill() may return False (fill_claims already claimed, or MAX() guard),
                                # but the row status still needs to be updated from 'placing'/'open' to reflect
                                # what the exchange actually says. Without this, placing rows stay stuck forever.
                                _place_cur.execute(
                                    "UPDATE bot_orders SET status=?, updated_at=? WHERE id=?",
                                    (new_status, int(time.time()), db_id)
                                )

                                seal_trade_state(bot_id)

                        else:

                            _place_cur.execute("UPDATE bot_orders SET status='failed', updated_at=? WHERE id=?",

                                               (int(time.time()), db_id))

                            logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → {o_status} (no fill). Marked failed.")

                    elif lookup_succeeded:

                        # Never reached the exchange — delete the intent row

                        _place_cur.execute("DELETE FROM bot_orders WHERE id=?", (db_id,))

                        logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → not on exchange. Deleted (never placed).")

                    else:
                        logger.warning(f"⚠️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → Lookup failed. Leaving DB row intact.")

                    break

                    break

                except Exception as e_r:

                    if "2015" in str(e_r):

                        # 🚀 BRIDGE FIX (V1.6.6): If permission denied, we cannot verify this order.

                        # If it's old, treat as stale/zombie to prevent deadlocking the bot.

                        if (int(time.time()) - 30) > 300: # 5 minutes old and unverifiable

                            _place_cur.execute("DELETE FROM bot_orders WHERE id=?", (db_id,))

                            logger.warning(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → Permission Denied & Stale. Cleanup forced.")

                    else:

                        logger.warning(f"[PRE-COMMIT-RESOLVE] Could not resolve row {db_id}: {e_r}")



        if placing_rows:

            _place_conn.commit()

        pass # _place_conn.close() disabled for singleton safety



        # 1.6. 🚀 HISTORY-BASED ORPHAN DETECTION

        # For any pair where physical position > virtual, scan 48h of exchange order history

        # for CQB_-prefixed fills that have no matching trade_history entry.

        # These are injected as bot_orders so the per-pair OFFLINE-SYNC picks them up below.

        try:

            _oh_conn = get_connection()

            _oh_cur = _oh_conn.cursor()

            _oh_cur.execute("SELECT pair, side, size FROM active_positions")

            phys_pos = {}

            for r in _oh_cur.fetchall():

                sym = _nsym(r[0])

                # Net physical quantity: Longs are positive, Shorts are negative

                size = float(r[2] or 0)

                side = str(r[1]).upper()

                signed_size = size if side == 'LONG' else -size

                phys_pos[sym] = phys_pos.get(sym, 0.0) + signed_size



            from engine.database import get_pair_virtual_net

            virt_pos = {}

            # Iterate over all symbols that have either a physical position or an active bot

            _oh_cur.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1")

            all_relevant_symbols = set(phys_pos.keys()) | { _nsym(r[0]) for r in _oh_cur.fetchall() }

            

            for sym in all_relevant_symbols:

                virt_pos[sym] = get_pair_virtual_net(sym)

            

            # 🚀 MANUAL WHITELIST INTEGRATION

            # Load all active manual whitelists to subtract from the gap calculation.

            # This prevents the system from flagging manual trades as orphans.

            _oh_cur.execute("SELECT pair, side, qty FROM manual_whitelists")

            raw_whitelists = _oh_cur.fetchall()

            merged_whitelists = {}

            for wp, ws, wq in raw_whitelists:

                # Merge into ticker-wide signed sum

                merged_whitelists[wp] = merged_whitelists.get(wp, 0.0) + (wq if ws == 'LONG' else -wq)



            gap_pairs = []

            all_symbols = set(phys_pos.keys()) | set(virt_pos.keys())

            for p in all_symbols:

                if pair_filter and p != pair_filter: continue

                pq = phys_pos.get(p, 0.0)

                vq = virt_pos.get(p, 0.0)

                

                # Adjust physical by whitelisted amount (Reality - Whitelisted = Managed Bot Reality)

                # If I have 1.0 physical and 1.0 is whitelisted, pq_adjusted = 0.0.

                wq = merged_whitelists.get(p, 0.0)

                pq_adjusted = pq - wq



                if abs(pq_adjusted - vq) > 0.001:
                    # If raw physical matches virtual without the whitelist (i.e. they are already in sync),
                    # then the whitelist is stale and can be cleared.
                    if abs(pq - vq) <= 0.001 and abs(wq) > 0.0:
                        clear_manual_whitelists_for_pair(p)
                        logger.info(f"🧹 [WHITELIST-CLEANUP] Raw physical matches virtual for {p} (phys={pq:.4f}, virt={vq:.4f}). Auto-cleared stale manual whitelists.")
                        wq = 0.0
                        pq_adjusted = pq
                    else:
                        gap_pairs.append((p, pq, vq, pq_adjusted - vq, 'NET'))
                elif abs(pq) < 0.0001 and abs(wq) > 0:
                    # 🧹 AUTO-CLEANUP: If physical position is gone, clear whitelists for this pair.
                    clear_manual_whitelists_for_pair(p)
                    logger.info(f"🧹 [WHITELIST-CLEANUP] Physical position for {p} is zero. Auto-cleared manual whitelists.")



            if gap_pairs:

                logger.info(f"🔍 [HISTORY-ORPHAN] {len(gap_pairs)} pairs with position gaps: {[(p, s, round(d,4)) for p,_,_,d,s in gap_pairs]}")



            # 🚀 OPTIMIZATION: Zero API Spam for Healthy Pairs

            # If a pair has exactly 0.0 mathematical gap AND no pair_filter was requested,

            # there is NO possible offline fill that altered inventory. Skip history API scan.

            pairs_to_check = set([p[0] for p in gap_pairs])

            

            if not pairs_to_check and not pair_filter:

                logger.info("✅ [OFFLINE-SYNC] All active pairs perfectly align with exchange reality. Zero gaps. Skipping history API scan.")

                return stats



            since_fallback = int((time.time() - since_hours * 3600) * 1000)  # Use injected since_hours instead of hardcoded 7 days to prevent rate limiting

            for gap_pair, phys_qty, v_qty, gap_qty, gap_side in gap_pairs:

                # ── v4.1.5 CROSS-CYCLE ORPHAN HEAL (local, before API scan) ─────────
                # For each gap pair, check if any bots have fills in cycles older than
                # trades.cycle_id that are unbalanced (no matching exit).  These are
                # invisible to standard recompute, causing a phantom gap.  Heal via
                # seal_trade_state(cycle_floor=...) before spending API quota.
                try:
                    _heal_conn = get_connection()
                    _heal_bots = _heal_conn.execute(
                        "SELECT t.bot_id, t.cycle_id FROM trades t "
                        "JOIN bots b ON b.id = t.bot_id "
                        "WHERE b.normalized_pair = ? AND b.is_active = 1 AND t.cycle_id IS NOT NULL",
                        (gap_pair,)
                    ).fetchall()
                    _healed_any = False
                    for _hbot_id, _hcycle in _heal_bots:
                        _healed = self._heal_local_crosscycle_orphans(_hbot_id, _hcycle)
                        if _healed > 0:
                            _healed_any = True
                    if _healed_any:
                        # Re-check virtual net after local heal to see if gap resolved
                        from engine.database import get_pair_virtual_net
                        _new_vq = get_pair_virtual_net(gap_pair)
                        _wq_adj = merged_whitelists.get(gap_pair, 0.0)
                        _new_gap = abs(phys_qty - _wq_adj - _new_vq)
                        if _new_gap < 0.001:
                            logger.info(
                                f"[CROSSCYCLE-HEAL] ✅ {gap_pair}: gap resolved by local "
                                f"cross-cycle heal (new_vq={_new_vq:.6f}, phys={phys_qty:.6f}). "
                                f"Skipping exchange history API scan for this pair."
                            )
                            continue
                except Exception as _ch_err:
                    logger.error(f"[CROSSCYCLE-HEAL] {gap_pair}: pre-scan heal error (non-fatal): {_ch_err}")
                # ─────────────────────────────────────────────────────────────────────

                for mt, ex in self.exchanges.items():

                    if not ex: continue


                    try:

                        hist = []

                        current_since = since_fallback

                        for _ in range(10):  # Fetch up to 10 pages to span long offline weekends

                            _ccxt_pair = _to_ccxt_pair(gap_pair, _all_pairs)

                            page = ex.fetch_closed_orders(_ccxt_pair, since=current_since, limit=1000)

                            if not page: break

                            hist.extend(page)

                            last_ts = max((o.get('timestamp') or 0) for o in page)

                            if last_ts <= current_since: break

                            current_since = last_ts + 1

                        

                        hist = sorted(hist, key=lambda x: x.get('timestamp') or 0)

                        _oi_conn = get_connection(); _oi_cur = _oi_conn.cursor()

                        for o in hist:

                            _trigger_orphan_hedge_signal = False

                            o_id = str(o.get('id') or o.get('order') or o.get('orderId') or '')

                            o_cid = o.get('clientOrderId') or (o.get('info') or {}).get('clientOrderId') or ''



                            # 🚀 ROOT CAUSE FIX: Binance occasionally strips clientOrderId from historical trades.

                            # If the CID is missing, we perform a reverse-lookup using Binance's native order_id 

                            # against our own database! This mathematically guarantees we perfectly adopt our own fills

                            # even when the API data stream degrades, ensuring true autonomy.

                            if not o_cid.startswith('CQB_') and o_id:

                                existing_row = _oi_cur.execute("SELECT client_order_id FROM bot_orders WHERE order_id=?", (o_id,)).fetchone()

                                if existing_row and existing_row[0] and existing_row[0].startswith('CQB_'):

                                    o_cid = existing_row[0]

                                    logger.info(f"🔍 [ID-RECOVERY] Recovered stripped CID {o_cid} via native orderId {o_id}")



                            is_forensic = False

                            if not o_cid.startswith('CQB_'):

                                if forensic_mode:

                                    # Match bot by pair

                                    attributed_bot_id = None

                                    for b_id, b_data in bot_states.items():

                                        if normalize_symbol(b_data.get('pair','')) == gap_pair:

                                            attributed_bot_id = b_id

                                            break

                                    

                                    if attributed_bot_id:

                                        o_side = str(o.get('side', '')).upper()

                                        b_dir = bot_states[attributed_bot_id].get('direction', '').upper()

                                        

                                        # Determine if this is an ADD or REDUCE forensic fill

                                        is_add = (o_side == 'BUY' and b_dir == 'LONG') or (o_side == 'SELL' and b_dir == 'SHORT')

                                        otype_r = 'forensic_adoption_add' if is_add else 'forensic_adoption_reduce'

                                        

                                        o_cid = f"CQB_{attributed_bot_id}_FORENSIC_{o_id}"

                                        is_forensic = True

                                        logger.info(f"🕵️‍♂️ [FORENSIC-MATCH] Attributed anonymous {o_side} fill ({o_id}) to Bot {attributed_bot_id} ({otype_r})")

                                    else:

                                        continue

                                else:

                                    continue



                            parts = o_cid.split('_')

                            try: 

                                if not is_forensic:

                                    attributed_bot_id = int(parts[1])

                            except (IndexError, ValueError): continue



                            # ── WIPE WALL GATE [v2.1] ──────────────────────────────────

                            # Do not import exchange history fills that predated a clean DB wipe.

                            # Even if the fill is a genuine gap, if it's older than the last

                            # reset (Market Close, TP, Force SL), it belongs to a dead session.

                            wipe_wall = bot_states.get(attributed_bot_id, {}).get('wipe_wall_ts', 0)

                            o_ts_ms = o.get('timestamp') or 0

                            if wipe_wall > 0 and o_ts_ms > 0 and (o_ts_ms / 1000) <= wipe_wall:

                                logger.debug(f"⏭️ [WIPE-WALL] Order {o_cid} (ts={o_ts_ms}) predates session boundary {wipe_wall}. Skipping history ghost.")

                                continue

                            # ─────────────────────────────────────────────────────────



                            o_status = (o.get('status') or '').lower()

                            o_filled = float(o.get('filled') or 0)

                            

                            # 🚀 ROOT CAUSE FIX: Binance FAPI API occasionally omits the 'filled' key 

                            # in its payload for fully executed Limit Grids. If status is filled but filledQty=0,

                            # fallback to the requested 'amount'.

                            if o_status in ('filled', 'closed') and o_filled <= 0:

                                o_filled = float(o.get('amount') or 0.0)

                                

                            o_price = float(o.get('average') or o.get('price') or 0)

                            

                            # Do not require 'filled'/'closed' status. A partial fill on a 'canceled' order is STILL A FILL.

                            if o_filled <= 0: continue



                            # Already linked to trade_history?

                            _oi_cur.execute("""SELECT COUNT(*) FROM trade_history

                                WHERE bot_id=? AND ABS(price-?)<? AND ABS(amount-?)<?

                                AND action IN ('WS_ENTRY_FILL','GRID_FILL','WS_GRID_FILL','WS_ENTRY_FILL')""",

                                (int(o_cid.split('_')[1]) if len(o_cid.split('_'))>1 and o_cid.split('_')[1].isdigit() else -1,

                                 o_price, o_price*0.002, o_filled, o_filled*0.002))

                            if _oi_cur.fetchone()[0] > 0: continue



                            # Parse bot_id from CID: CQB_{bot_id}_TYPE_{step}_{ts}

                            parts = o_cid.split('_')

                            try: attributed_bot_id = int(parts[1])

                            except (IndexError, ValueError): continue



                            _oi_cur.execute("SELECT id, filled_amount, status, cycle_id FROM bot_orders WHERE order_id=?", (o_id,))

                            existing_order = _oi_cur.fetchone()

                            

                            is_orphan_insert = False

                            is_revive = False

                            if existing_order:

                                ex_id, ex_filled, ex_status, ex_cyc = existing_order

                                # 🚀 ROOT CAUSE FIX: If the existing row is 'reset_cleared',

                                # it means this order was INTENTIONALLY wiped (e.g. system wipe, manual reset).

                                # We must NEVER revive a reset_cleared order, otherwise we undo the wipe

                                # and inflate the ledger with ancient dead cycles.

                                if ex_status == 'reset_cleared':

                                    logger.debug(f"⏭️ [REVIVE-SKIP] Order {o_cid}: row is explicitly reset_cleared. Not reviving.")

                                    continue

                                elif float(ex_filled or 0) <= 0 and o_filled > 0:

                                    from engine.parity_gates import gate_heal_fill_qty, gate_heal_exit_without_entry
                                    from engine.ledger import credit_fill, seal_trade_state

                                    _raw_otype = parts[2].upper() if len(parts) > 2 else 'GRID'
                                    if _raw_otype == 'DUST':
                                        _raw_otype = 'DUST_CLOSE'
                                    _heal_otype = (
                                        _raw_otype.lower()
                                        if _raw_otype in ('ENTRY', 'GRID', 'TP', 'HEDGETP', 'HEDGE', 'DUST_CLOSE', 'SL', 'CLOSE')
                                        else 'grid'
                                    )
                                    if not gate_heal_exit_without_entry(
                                        attributed_bot_id, _heal_otype, float(o_filled)
                                    ):
                                        continue

                                    _allow = gate_heal_fill_qty(
                                        gap_pair,
                                        float(o_filled),
                                        exchange=self.exchanges.get('future'),
                                    )
                                    if _allow <= 0:
                                        continue

                                    logger.info(
                                        f"🩹 [HEALING] Order {o_cid}: crediting {_allow:.6f} "
                                        f"(order fill {o_filled:.6f}) via credit_fill."
                                    )
                                    credit_fill(
                                        bot_id=attributed_bot_id,
                                        order_id=str(o_id),
                                        cumulative_qty=_allow,
                                        avg_price=o_price,
                                        order_type=_heal_otype,
                                        is_cumulative=True,
                                    )
                                    seal_trade_state(attributed_bot_id)
                                    continue  # Healed in place, no insert needed

                                else:

                                    continue  # Already properly credited — skip

                            else:

                                is_orphan_insert = True



                            if is_forensic:

                                # otype_r was already set in the forensic block above

                                pass 

                            else:

                                raw_otype = parts[2].upper() if len(parts)>2 else 'GRID'
                                if raw_otype == 'DUST':
                                    raw_otype = 'DUST_CLOSE'

                                otype_r = raw_otype if raw_otype in ('ENTRY','GRID','TP','HEDGETP','HEDGE','DUST_CLOSE') else 'GRID'



                            _oi_cur.execute(

                                "SELECT COALESCE(cycle_id,1), basket_start_time, "

                                "COALESCE(cycle_start_time, 0) FROM trades WHERE bot_id=?",

                                (attributed_bot_id,)

                            )

                            cr = _oi_cur.fetchone()

                            if cr:

                                cyc_db = cr[0] or 1

                                bst    = cr[1] or 0  # engine-operation timestamp (EE timer)

                                cst    = cr[2] or 0  # exchange-event-anchored cycle boundary (v2.1.0)

                            else:

                                cyc_db, bst, cst = 1, 0, 0

                            # Extract cycle_id and step from clientOrderId if possible (CQB_{bot_id}_{type}_{cycle_id}_{step})
                            is_legacy_cid = True
                            cyc_from_cid = None
                            step_from_cid = None

                            if len(parts) >= 5 and parts[2].upper() in ('ENTRY', 'GRID', 'TP', 'HEDGE', 'HEDGETP', 'SL', 'CLOSE'):
                                if parts[3].isdigit():
                                    cyc_from_cid = int(parts[3])
                                    step_str = parts[4]
                                    import re
                                    m = re.match(r'^(\d+)', step_str)
                                    if m:
                                        step_from_cid = int(m.group(1))
                                        is_legacy_cid = False

                            if not is_legacy_cid:
                                cyc = cyc_from_cid
                                step_g = step_from_cid
                            else:
                                cyc = cyc_db
                                step_g = int(parts[3]) if len(parts)>3 and parts[3].isdigit() else 1


                            # ── CYCLE POISONING GUARD (v2.1.0) ──────────────────────────────────

                            # Uses cycle_start_time (CST) as the PRIMARY boundary — this is the

                            # exact exchange fill timestamp that ended the previous cycle.

                            # Falls back to basket_start_time (BST) if CST is not yet available.                            # RULE: A fill that predates the cycle boundary by >60s belongs to

                            # a PAST cycle and must be demoted (cycle_id - 1).

                            # EXCEPTION: bst==0 AND cst==0 → no boundary yet → no demotion.

                            # EXCEPTION: is_revive → always stay in current cycle.

                            o_ts = o.get('timestamp') or 0

                            if not is_revive:

                                # Prefer cycle_start_time; fall back to basket_start_time

                                effective_boundary = cst if cst > 0 else bst

                                if effective_boundary > 0 and o_ts > 0 and o_ts < (effective_boundary * 1000 - 60000):

                                    if is_orphan_insert:

                                        # 🚀 ZOMBIE DEFENSE: If we don't have this fill in our DB AND it's ancient,

                                        # do NOT adopt it. It belongs to a dead session/cycle.

                                        logger.debug(f"⏭️ [ZOMBIE-SKIP] Bot {attributed_bot_id}: fill {o_cid} is ancient ({o_ts}). Not reviving orphan.")

                                        continue

                                    

                                    # Fill is clearly older than the authoritative cycle boundary.
                                    # Only demote if it's a legacy CID where we had to guess the cycle.
                                    if is_legacy_cid:

                                        cyc = max(0, cyc - 1)

                                        logger.debug(

                                            f"[CYCLE-GUARD] Bot {attributed_bot_id}: fill ts={o_ts}ms is "

                                            f"{(effective_boundary*1000 - o_ts)/1000:.0f}s before boundary "

                                            f"({'CST' if cst > 0 else 'BST'} ts={effective_boundary}). "

                                            f"Demoting legacy CID to cycle {cyc}."

                                        )

                                # effective_boundary==0 → no boundary yet → fill stays in current cycle

                            

                            # STATUS FIX: Insert as terminal status so recompute_invested_from_orders counts it

                            final_status = o_status if o_status in ('filled', 'closed', 'canceled', 'cancelled') else 'filled'

                            

                            # Record fill timestamp for the new bot_orders row

                            orphan_fill_ts = int((o.get('lastTradeTimestamp') or o.get('timestamp') or time.time()*1000) / 1000)



                            if is_orphan_insert:

                                # ── CID-LEVEL DEDUPLICATION GUARD (v2.3.5) ──────────────────────

                                # The DB check above (line ~555) only deduplicates by exchange order_id.

                                # But the same CQB_ client_order_id can legitimately appear on multiple

                                # exchange orders when the GTX retry loop cancels one and places another

                                # for the same logical step. If the first (cancelled) order had a partial

                                # fill AND the second (filled) order is also credited, inserting the

                                # cancelled one as a history-orphan would DOUBLE-COUNT the position qty.

                                            # Guard: if any existing row with the same (bot_id, cycle_id, CID,

                                # entry-type) already has filled_amount > 0, this fill was already

                                # credited — skip insertion to prevent open_qty inflation.

                                _cid_dup_check = _oi_cur.execute(

                                    """SELECT id, filled_amount FROM bot_orders

                                       WHERE bot_id=? AND cycle_id=? AND client_order_id=?

                                         AND order_type IN ('entry','grid','adoption','adoption_add','tp','close')

                                         AND filled_amount > 0

                                         AND order_id != ?""",

                                    (attributed_bot_id, cyc, o_cid, o_id)

                                ).fetchone()

                                if _cid_dup_check:

                                    logger.info(

                                        f"⏭️ [CID-DEDUP] Skipping history-orphan insert for {o_cid} (order_id={o_id}): "

                                        f"CID already credited via row id={_cid_dup_check[0]} qty={_cid_dup_check[1]}. "

                                        f"Preventing double-count of {o_filled} qty."

                                    )

                                else:
                                    _cid_exists = _oi_cur.execute(
                                        "SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND client_order_id = ?",
                                        (attributed_bot_id, o_cid)
                                    ).fetchone()[0] > 0
                                    if _cid_exists:
                                        base_cid = o_cid
                                        o_cid = f"{base_cid}_{int(time.time())}"
                                        suffix_idx = 1
                                        while True:
                                            _check = _oi_cur.execute(
                                                "SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND client_order_id = ?",
                                                (attributed_bot_id, o_cid)
                                            ).fetchone()[0]
                                            if _check == 0:
                                                break
                                            o_cid = f"{base_cid}_{int(time.time())}_{suffix_idx}"
                                            suffix_idx += 1

                                    _oi_cur.execute("""INSERT OR IGNORE INTO bot_orders

                                        (bot_id,step,order_type,order_id,price,amount,filled_amount,status,created_at,updated_at,client_order_id,notes,cycle_id,filled_at)

                                        VALUES (?,?,?,?,?,?,?,?,?,?,?,'history-orphan',?,?)""",

                                        (attributed_bot_id, step_g, otype_r.lower(), o_id, o_price, o_filled, o_filled, final_status,

                                         int((o.get('timestamp') or time.time()*1000)/1000), int(time.time()), o_cid, cyc,

                                         orphan_fill_ts))

                                    if _oi_cur.rowcount > 0:
                                        logger.info(f"   ➕ [HISTORY-ORPHAN] Inserted missing bot_order: Bot {attributed_bot_id} {otype_r} qty={o_filled}@{o_price} order_id={o_id} cycle={cyc} filled_at={orphan_fill_ts}")
                                        _trigger_orphan_hedge_signal = True
                                    else:
                                        _trigger_orphan_hedge_signal = False



                            elif is_revive:

                                _oi_cur.execute("""UPDATE bot_orders 

                                    SET status=?, cycle_id=?, updated_at=?, filled_amount=?, filled_at=?

                                    WHERE id=?""", 

                                    (final_status, cyc, int(time.time()), o_filled, orphan_fill_ts, ex_id))

                                logger.info(f"   🔄 [REVIVE-UPDATE] Updated existing bot_order to 'filled' for cycle {cyc}: Bot {attributed_bot_id} {otype_r} qty={o_filled}@{o_price} order_id={o_id}")

                                _trigger_orphan_hedge_signal = True

                                # REVIVE INTEGRITY: Align trades.cycle_id + stamp cycle_start_time

                                # so future recomputes have a valid, exchange-anchored boundary

                                # and don't re-demote.

                                _oi_cur.execute(

                                    "UPDATE trades SET cycle_id=?, "

                                    "basket_start_time=COALESCE(NULLIF(basket_start_time,0), ?), "

                                    "cycle_start_time=COALESCE(NULLIF(cycle_start_time,0), ?) "

                                    "WHERE bot_id=? AND (cycle_id IS NULL OR cycle_id != ?)",

                                    (cyc, int(time.time()), orphan_fill_ts, attributed_bot_id, cyc))

                                if _oi_cur.rowcount > 0:

                                    logger.info(f"🔄 [REVIVE-ALIGN] Set trades.cycle_id={cyc}, cycle_start_time={orphan_fill_ts} for Bot {attributed_bot_id}.")

                            elif cst == 0:

                                # Non-revive adoption into a bot with no cycle_start_time yet:

                                # stamp it now with the actual fill timestamp from the exchange

                                # so the NEXT reconciler run has a proper boundary.

                                _oi_cur.execute(

                                    "UPDATE trades SET cycle_start_time=? "

                                    "WHERE bot_id=? AND (cycle_start_time IS NULL OR cycle_start_time=0)",

                                    (orphan_fill_ts, attributed_bot_id))

                                if _oi_cur.rowcount > 0:

                                    logger.info(f"🕐 [CST-STAMP] Stamped cycle_start_time={orphan_fill_ts} for Bot {attributed_bot_id} on adoption (v2.1.0).")

                                # Also stamp bst if still 0, for backward compatibility with EE timer

                                _oi_cur.execute(

                                    "UPDATE trades SET basket_start_time=? "

                                    "WHERE bot_id=? AND (basket_start_time IS NULL OR basket_start_time=0)",

                                    (int(time.time()), attributed_bot_id))

                            # 🛡️ HEDGE CHILD SIGNAL (OFFLINE PATH - HISTORY ORPHAN)
                            if _trigger_orphan_hedge_signal and otype_r.lower() in ('entry', 'grid'):
                                try:
                                    _bot_row = _oi_cur.execute(
                                        "SELECT name, pair, direction, hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = ?",
                                        (attributed_bot_id,)
                                    ).fetchone()
                                    if _bot_row:
                                        _bname, _bpair, _bdir, _child_id, _trigger = _bot_row
                                        if _child_id and _trigger and _trigger > 0:
                                            if step_g >= _trigger:
                                                # Get step qty
                                                _step_qty_row = _oi_cur.execute(
                                                    """SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders
                                                       WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                                       AND order_type IN ('entry', 'grid')
                                                       AND status IN ('filled', 'partially_filled', 'closed')
                                                       AND filled_amount > 0""",
                                                    (attributed_bot_id, step_g, cyc)
                                                ).fetchone()
                                                _step_qty = float(_step_qty_row[0] or 0.0) if _step_qty_row else o_filled

                                                # Get actual fill price
                                                _price_row = _oi_cur.execute(
                                                    """SELECT ROUND(SUM(filled_amount * price) / NULLIF(SUM(filled_amount), 0), 8)
                                                       FROM bot_orders
                                                       WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                                       AND order_type IN ('entry', 'grid')
                                                       AND status IN ('filled', 'partially_filled', 'closed')
                                                       AND filled_amount > 0""",
                                                    (attributed_bot_id, step_g, cyc)
                                                ).fetchone()
                                                _hist_price = float(_price_row[0] or 0.0) if _price_row else 0.0
                                                _step_fill_price = _hist_price if _hist_price > 0 else o_price

                                                from engine.bot_executor import BotExecutor
                                                executor = BotExecutor(runner=None)
                                                logger.info(
                                                    f"[HEDGE-OFFLINE] Synchronously signaling hedge child {_child_id} "
                                                    f"for parent step {step_g} fill (orphan). Price={_step_fill_price:.4f}"
                                                )
                                                executor._signal_hedge_child_entry(
                                                    parent_bot_id=attributed_bot_id,
                                                    parent_name=_bname,
                                                    parent_step=step_g,
                                                    pair=_bpair,
                                                    direction=_bdir,
                                                    step_qty=_step_qty,
                                                    step_fill_price=_step_fill_price,
                                                    exchange=ex,
                                                    parent_cycle_id=cyc,
                                                    current_price=o_price
                                                )
                                except Exception as _hedge_err:
                                    logger.error(f"❌ Failed to evaluate offline hedge child signal for bot {attributed_bot_id}: {_hedge_err}")

                        _oi_conn.commit(); pass # _oi_conn.close() disabled for singleton safety

                        break

                    except Exception as oe: logger.warning(f"[HISTORY-ORPHAN] {gap_pair}: {oe}")

        except Exception as ohe: logger.warning(f"[HISTORY-ORPHAN] outer: {ohe}")



        # 2. Scan History per Pair — PARALLEL FETCH for performance

        # Each pair independently fetches its exchange history. We use threading because

        # these are pure I/O bound calls (no shared state during fetch). The DB writes

        # per pair are atomic and independent, so parallelism is safe.

        since_ts = int((time.time() - (since_hours * 3600)) * 1000)



        from concurrent.futures import ThreadPoolExecutor, as_completed



        def _fetch_pair_history(pair):

            """Fetch and return (pair, combined_history_list) for a single pair."""

            for mt, ex in self.exchanges.items():

                if not ex:

                    continue

                try:

                    _ccxt_pair = _to_ccxt_pair(pair, _all_pairs)

                    history = ex.fetch_closed_orders(_ccxt_pair, limit=100) # Only recent to prevent ghost-collision spam

                    if not isinstance(history, list):

                        history = []

                    # Also fetch open orders for partial fill recovery

                    try:

                        open_orders = ex.fetch_open_orders(_ccxt_pair)

                        if isinstance(open_orders, list):

                            history.extend(open_orders)

                    except Exception as eo_e:

                        logger.error(f"Failed to fetch open orders for {pair}: {eo_e}")

                    return pair, history, ex

                except Exception as e:

                    logger.error(f"Failed to fetch history for {pair}: {e}")

            return pair, [], None



        pair_results = {}

        with ThreadPoolExecutor(max_workers=min(8, len(pairs_to_check) or 1), thread_name_prefix="fill-scan") as pool:

            futures = {pool.submit(_fetch_pair_history, p): p for p in pairs_to_check}

            for fut in as_completed(futures):

                try:

                    pair, history, ex = fut.result()

                    pair_results[pair] = (history, ex)

                except Exception as e:

                    logger.error(f"Parallel fetch failed for a pair: {e}")



        for pair in pairs_to_check:

            history, ex = pair_results.get(pair, ([], None))

            if not history or ex is None:

                continue

            # 🚀 CHRONOLOGICAL PROCESSING FIX

            # Binance returns history descending (newest first). This caused offline TP fills

            # to be processed BEFORE their prerequisite Entries/Grids, causing the

            # reset_bot_after_tp hook to calculate `net_qty` on an incomplete ledger!

            history = sorted(history, key=lambda x: x.get('timestamp') or 0)



            if not history:

                continue

                

            # Sort by time (Oldest first) to replay history

            history.sort(key=lambda x: x.get('timestamp', 0) or 0)

                    

            conn = get_connection()

            cursor = conn.cursor()

            

            for order in history:

                o_id = str(order.get('id') or order.get('order') or order.get('orderId') or '')

                cid = order.get('clientOrderId', '')



                # 🚀 ROOT CAUSE FIX: Recover stripped IDs via native orderId

                if not cid.startswith('CQB_') and o_id:

                    existing_row = cursor.execute("SELECT client_order_id FROM bot_orders WHERE order_id=?", (o_id,)).fetchone()

                    if existing_row and existing_row[0] and existing_row[0].startswith('CQB_'):

                        cid = existing_row[0]

                        logger.info(f"🔍 [ID-RECOVERY] Recovered stripped CID {cid} via native orderId {o_id} in offline sync")



                if not cid.startswith('CQB_'): continue



                # Parse CID: CQB_{bot_id}_{type}_{step}

                # Examples: CQB_1_ENTRY_0, CQB_1_GRID_1, CQB_1_TP_5

                try:

                    parts = cid.split('_')

                    if len(parts) < 4: continue

                    

                    bot_id = int(parts[1])

                    otype = parts[2].lower() # ENTRY, GRID, TP

                    # Support both CQB_{bot_id}_{type}_{step} (legacy) and CQB_{bot_id}_{type}_{cycle_id}_{step}

                    if len(parts) >= 5:

                        step = int(parts[4])

                    else:

                        step = int(parts[3])

                    

                    current_state = bot_states.get(bot_id, {})

                    # ── WIPE WALL GATE ──
                    wipe_wall = current_state.get('wipe_wall_ts', 0)
                    o_ts_ms = order.get('timestamp') or 0
                    if wipe_wall > 0 and o_ts_ms > 0 and (o_ts_ms / 1000) <= wipe_wall:
                        continue
                    # ────────────────────


                    

                    # 🔑 CYCLE-ID GUARD: The definitive, time-free fill isolation check.

                    # An offline fill is ONLY adopted if it belongs to the bot's CURRENT cycle.

                    # A fill from a previous cycle will have no matching bot_orders row with the

                    # current cycle_id, OR will have a 'reset_cleared' status. Both are rejected.

                    # [v3.5.5] Surgical CID lookup: match by order_id first, then
                    # restrict client_order_id match to the CURRENT cycle only.
                    # This prevents a historical reset_cleared row from a past cycle
                    # (same CID, different order_id) from blocking the new fill.
                    bot_current_cycle_check = current_state.get('cycle_id', 1)
                    cursor.execute("""
                        SELECT status, cycle_id, filled_amount, order_id FROM bot_orders 
                        WHERE (order_id=? OR (client_order_id=? AND cycle_id=?))
                        ORDER BY filled_amount DESC LIMIT 1
                    """, (order['id'], cid, bot_current_cycle_check))

                    row = cursor.fetchone()

                    # [v3.5.5] If matched by CID alone (different exchange order_id),
                    # this is a different physical order — treat as new, don't skip.
                    if row and row[3] is not None and str(row[3]) != '' and str(row[3]) != str(order['id']):
                        logger.debug(
                            f"[CID-NEW-ORDER] CID {cid} matched DB row (order_id={row[3]}) but exchange order_id={order['id']} differs. "
                            f"Treating as new physical fill."
                        )
                        row = None



                    logger.info(f"🔍 [OFFLINE-SYNC] Checking Order {order['id']} (CID: {cid}) | InDB: {'Yes' if row else 'No'} | DBStatus: {row[0] if row else 'N/A'} | DBCycle: {row[1] if row else 'N/A'} | DBFilled: {row[2] if row else 'N/A'}")



                    if row:

                        # Known order: reject if already processed or from old cycle

                        if row[0] in ['reset_cleared', 'auto_closed']:

                            continue

                        if row[0] in ['filled', 'closed'] and float(row[2] or 0) > 0.0001:

                            continue

                            

                        # Also reject if it has a different cycle_id than the bot's current cycle

                        bot_current_cycle = current_state.get('cycle_id', 1)

                        if row[1] is not None and row[1] != bot_current_cycle:

                            logger.debug(f"🛑 [CYCLE-GUARD] Rejecting fill {cid}: belongs to cycle {row[1]}, bot is on cycle {bot_current_cycle}.")

                            continue

                    else:

                        order_ts_sec = int((order.get('timestamp') or order.get('lastTradeTimestamp') or 0) / 1000)

                        bot_start = current_state.get('basket_start_time', 0)

                        _guard_is_restart_fallback = (bot_start > 0 and bot_start >= (ENGINE_START_TIME - 600))

                        if bot_start > 0 and not _guard_is_restart_fallback and order_ts_sec < (bot_start - 60):

                            logger.debug(f"🛑 [DNA-GUARD] Rejecting order {cid}: matches DNA but happened before basket start (ts={order_ts_sec} < bot_start={bot_start}).")

                            continue

                        # 🚀 ROOT CAUSE FIX (v2.1.1): Do NOT reject DNA-matched fills when bot_start==0.

                        # bot_start==0 means basket_start_time was cleared (DNA-WIPE after failed recompute,

                        # or a brand-new bot). In that state we have NO valid time boundary — the CQB_

                        # client-order-ID DNA is the ONLY proof of ownership and it already passed above.

                        # The old 1-hour hard-cutoff caused a silent oscillation:

                        #   DNA-WIPE clears BST → next pass: bot_start=0 → 1h rule drops fills →

                        #   recompute still returns 0 → DNA-WIPE fires again → infinite loop.

                        # Fix: when bot_start==0, trust the DNA proof unconditionally (no time filter).

                        # The wipe_wall gate (above) already prevents ghost fills from dead sessions.

                        logger.info(f"✅ DNA MATCH: Order {cid} matches Bot {bot_id} DNA. Authorizing processing despite DB state.")



                    # Update Bot State

                    order_status = order.get('status', '').lower()

                    

                    # GRACE PERIOD GUARD

                    finish_ts = order.get('lastTradeTimestamp') or order.get('timestamp') or 0

                    if order_status in ['closed', 'filled']:

                        if (time.time() * 1000 - finish_ts) < 60000:

                            logger.debug(f"⏳ [GRACE PERIOD] Skipping recently filled order {cid} (Age: {int((time.time()*1000 - finish_ts)/1000)}s); giving WS time to process.")

                            continue

                    elif order_status == 'open':

                        if (time.time() * 1000 - finish_ts) < 10000:

                            continue

                        if not order.get('filled') or float(order.get('filled')) <= 0:

                            continue

                    

                    fill_price = float(order.get('average') or order.get('price') or 0.0)

                    fill_qty = float(order.get('filled') or 0.0)



                    # 🚀 ROOT CAUSE FIX: CCXT/Binance testnet omits 'filled' key for fully filled Limit Orders!

                    if order_status in ('filled', 'closed') and fill_qty <= 0:

                        fill_qty = float(order.get('amount') or 0.0)



                    if order_status in ('filled', 'closed') and fill_qty <= 0:

                        logger.debug(f"⏭️ [OFFLINE-SYNC] Skipping {cid}: status=filled but amount=0. Ignoring empty execution.")

                        continue



                    if order_status in ['canceled', 'cancelled', 'expired', 'rejected']:

                        logger.debug(f"🧹 [OFFLINE-SYNC] Syncing cancelled order {cid} (status={order_status}) to DB.")

                        cursor.execute("UPDATE bot_orders SET status=?, updated_at=? WHERE order_id=?",

                                       (order_status, int(time.time()), order['id']))

                        if fill_qty <= 0:

                            continue

                        else:

                            logger.info(f"⚡ [OFFLINE-RECOVERY] {cid} was {order_status} but has PARTIAL FILL ({fill_qty}). Authorizing step advancement.")

                    elif order_status not in ('filled', 'closed'):

                        if fill_qty > 0:

                            logger.info(f"⚡ [OFFLINE-RECOVERY] {cid} was {order_status} but has PARTIAL FILL ({fill_qty}). Authorizing partial account.")

                        else:

                            logger.debug(f"Skipping non-filled order {cid} (status={order_status})")

                            continue

                    fill_symbol = order.get('symbol', pair)

                    bot_name = f"Bot-{bot_id}"

                    curr_step = current_state.get('current_step', 0)



                    logger.info(f"🕵️ RECONSTRUCTING: Found Fill for Bot {bot_id} {otype} {step} @ {fill_price} for {fill_symbol}")



                    # 🔑 PRECISION: Write order to DB BEFORE processing state effects

                    cursor.execute("SELECT id, filled_amount FROM bot_orders WHERE order_id=?", (order['id'],))

                    existing = cursor.fetchone()

                    

                    previously_filled = float(existing[1] or 0.0) if existing else 0.0

                    unaccounted_qty = max(0.0, fill_qty - previously_filled)

                    

                    if existing:

                        # [v3.5.5] Target only order_id — avoid corrupting other rows
                        # (e.g. virtual_netting rows) that share the same client_order_id prefix.
                        cursor.execute("UPDATE bot_orders SET status='filled', updated_at=?, filled_amount=? WHERE order_id=?",

                                       (int(time.time()), fill_qty, order['id']))

                    else:

                        cursor.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,))

                        _cycle_row = cursor.fetchone()

                        _bot_cycle = _cycle_row[0] if _cycle_row and _cycle_row[0] else 1

                        cursor.execute("""

                            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, updated_at, client_order_id, notes, cycle_id)

                            VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)

                        """, (bot_id, step, otype.lower(), order['id'], fill_price, fill_qty, fill_qty,

                              int(order.get('timestamp', time.time()*1000)/1000), int(time.time()), cid, 'Reconstructed from History', _bot_cycle))



                    if otype == 'tp':

                        if fill_price <= 0:

                            logger.warning(f"⚠️ Skipping OFFLINE_TP for Bot {bot_id}: fill_price={fill_price} is invalid.")

                            continue

                            

                        # 🚀 ROOT CAUSE FIX: Do not fully reset the bot if this is only a partial offline TP fill!

                        final_status = order_status if order_status in ('filled', 'closed', 'canceled', 'cancelled') else 'filled'

                        

                        # Existing is index 5 for amount? Let's assume order_status drives the final state

                        is_fully_closed = (order_status in ('filled', 'closed'))

                        

                        if is_fully_closed:

                            self._handle_offline_tp_fill(cursor, bot_id, bot_name, fill_price, fill_symbol, exchange=ex)

                            stats['tp_fills'] += 1

                        else:

                            # It is a partial TP offline!

                            from engine.database import log_trade

                            log_trade(bot_id, 'OFFLINE_TP_PARTIAL', fill_symbol, fill_price, unaccounted_qty, 0.0, f"TP_PARTIAL")

                            logger.info(f"📋 OFFLINE TP Partial: Bot {bot_id} sold {unaccounted_qty:.6f} @ {fill_price}. DB natively tracking partial closure.")

                            stats['tp_fills'] += 1

                        

                    elif otype == 'grid':

                        if step >= curr_step:

                            cursor.execute(

                                "SELECT COUNT(*) FROM bot_orders WHERE order_id=? AND status IN ('filled','closed')",

                                (order['id'],)

                            )

                            already_in_orders = cursor.fetchone()[0] > 0

                            if unaccounted_qty <= 1e-8 and already_in_orders:

                                logger.info(f"⏭️ [OFFLINE-DEDUP] Skipping fill for Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — already fully accounted for.")

                                # [v3.5.5] Target only order_id to avoid corrupting sibling rows.
                                cursor.execute("UPDATE bot_orders SET status='filled' WHERE order_id=?", (order['id'],))

                                continue

                            logger.info(f"✅ Re-playing OFFLINE_GRID for Bot {bot_id} (Step {step}) for {unaccounted_qty:.6f} unaccounted qty.")

                            self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, unaccounted_qty, curr_step, current_state.get('total_invested',0), current_state.get('avg_entry',0), fill_symbol)

                            stats['grid_fills'] += 1

                            old_inv = bot_states[bot_id].get('total_invested', 0)

                            old_avg = bot_states[bot_id].get('avg_entry', 0)

                            fill_cost = fill_price * unaccounted_qty

                            new_inv = old_inv + fill_cost

                            new_avg = ((old_inv * old_avg) + fill_cost) / new_inv if new_inv > 0 else fill_price

                            bot_states[bot_id]['current_step'] = step

                            bot_states[bot_id]['total_invested'] = new_inv

                            bot_states[bot_id]['avg_entry'] = new_avg

                            

                    elif otype == 'entry':

                        if curr_step == 0:

                            if unaccounted_qty > 1e-8:

                                logger.info(f"✅ Re-playing OFFLINE_ENTRY for Bot {bot_id} for {unaccounted_qty:.6f} unaccounted qty.")

                                order_time_sec = int(order.get('timestamp', time.time() * 1000) / 1000)

                                self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, unaccounted_qty, fill_symbol, order_time_sec)

                                stats['entry_fills'] += 1

                                bot_states[bot_id]['current_step'] = 1

                                bot_states[bot_id]['entry_confirmed'] = 1

                                old_inv = bot_states[bot_id].get('total_invested', 0)

                                bot_states[bot_id]['total_invested'] = old_inv + (fill_price * unaccounted_qty)

                            else:
                                # ── Stale-Trades Recovery (v4.1.3) ────────────────────────────────
                                # unaccounted_qty=0: bot_orders already has filled_amount=fill_qty,
                                # so the fill IS in the ledger. But if trades.open_qty=0, the fill
                                # was never propagated to trades — this happens when a fill arrives
                                # while the bot is in REQUIRE_MANUAL_PROOF (fill-credit is not
                                # blocked in WS but credit_fill returns False because the cycle
                                # advanced). Detect and heal by rolling cycle_id back to the fill's
                                # cycle and resealing.
                                bo_cycle = order.get('_db_cycle_id')  # populated below if available
                                if not bo_cycle:
                                    _bo_row = cursor.execute(
                                        "SELECT cycle_id FROM bot_orders WHERE order_id=?",
                                        (o_id,)
                                    ).fetchone()
                                    bo_cycle = _bo_row[0] if _bo_row else None

                                current_open_qty = current_state.get('open_qty', 0.0)
                                if bo_cycle and current_open_qty <= 1e-8 and fill_qty > 1e-8:
                                    logger.warning(
                                        f"[OFFLINE-STALE-TRADES] Bot {bot_id}: bot_orders has "
                                        f"entry fill {o_id} (qty={fill_qty:.6f}, cycle={bo_cycle}) "
                                        f"but trades.open_qty=0. Rolling cycle_id to {bo_cycle} "
                                        f"and resealing — likely silenced-fill from REQUIRE_MANUAL_PROOF."
                                    )
                                    cursor.execute(
                                        "UPDATE trades SET cycle_id=? WHERE bot_id=?",
                                        (bo_cycle, bot_id)
                                    )
                                    conn.commit()
                                    try:
                                        from engine.ledger import seal_trade_state
                                        seal_result = seal_trade_state(bot_id, force_recompute=True)
                                        logger.info(
                                            f"[OFFLINE-STALE-TRADES] Bot {bot_id}: reseal after "
                                            f"cycle_id rollback → {seal_result}"
                                        )
                                        stats['entry_fills'] += 1
                                        bot_states[bot_id]['current_step'] = 1
                                        bot_states[bot_id]['entry_confirmed'] = 1
                                        bot_states[bot_id]['total_invested'] = seal_result.get('cost', 0.0)
                                        bot_states[bot_id]['open_qty'] = seal_result.get('qty', 0.0)
                                    except Exception as _seal_err:
                                        logger.error(
                                            f"[OFFLINE-STALE-TRADES] Bot {bot_id}: reseal failed: {_seal_err}"
                                        )

                    # Check if a new fill was credited/re-played in this iteration
                    _new_fill_credited = False
                    if otype == 'grid' and step >= curr_step:
                        if not (unaccounted_qty <= 1e-8 and already_in_orders):
                            _new_fill_credited = True
                    elif otype == 'entry' and curr_step == 0 and unaccounted_qty > 1e-8:
                        _new_fill_credited = True

                    if _new_fill_credited:
                        # 🛡️ HEDGE CHILD SIGNAL (OFFLINE PATH)
                        try:
                            _bot_row = cursor.execute(
                                "SELECT name, pair, direction, hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = ?",
                                (bot_id,)
                            ).fetchone()
                            if _bot_row:
                                _bname, _bpair, _bdir, _child_id, _trigger = _bot_row
                                if _child_id and _trigger and _trigger > 0:
                                    if step >= _trigger:
                                        _cycle_row = cursor.execute("SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
                                        _parent_cycle = _cycle_row[0] if _cycle_row else 1

                                        _step_qty_row = cursor.execute(
                                            """SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders
                                               WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                               AND order_type IN ('entry', 'grid')
                                               AND status IN ('filled', 'partially_filled', 'closed')
                                               AND filled_amount > 0""",
                                            (bot_id, step, _parent_cycle)
                                        ).fetchone()
                                        _step_qty = float(_step_qty_row[0] or 0.0) if _step_qty_row else unaccounted_qty

                                        _price_row = cursor.execute(
                                            """SELECT ROUND(SUM(filled_amount * price) / NULLIF(SUM(filled_amount), 0), 8)
                                               FROM bot_orders
                                               WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                               AND order_type IN ('entry', 'grid')
                                               AND status IN ('filled', 'partially_filled', 'closed')
                                               AND filled_amount > 0""",
                                            (bot_id, step, _parent_cycle)
                                        ).fetchone()
                                        _hist_price = float(_price_row[0] or 0.0) if _price_row else 0.0
                                        _step_fill_price = _hist_price if _hist_price > 0 else fill_price

                                        from engine.bot_executor import BotExecutor
                                        executor = BotExecutor(runner=None)
                                        logger.info(
                                            f"[HEDGE-OFFLINE] Synchronously signaling hedge child {_child_id} "
                                            f"for parent step {step} fill. Historical Price={_step_fill_price:.4f}"
                                        )
                                        executor._signal_hedge_child_entry(
                                            parent_bot_id=bot_id,
                                            parent_name=_bname,
                                            parent_step=step,
                                            pair=_bpair,
                                            direction=_bdir,
                                            step_qty=_step_qty,
                                            step_fill_price=_step_fill_price,
                                            exchange=ex,
                                            parent_cycle_id=_parent_cycle,
                                            current_price=fill_price
                                        )
                        except Exception as _hedge_err:
                            logger.error(f"❌ Failed to evaluate offline hedge child signal for bot {bot_id}: {_hedge_err}")



                        

                except Exception as e:

                    logger.debug(f"Error parsing/processing CID {cid}: {e}")

                    continue



            # ── STARTUP VERIFICATION OF PENDING/NEW ORDERS FROM PREVIOUS SESSION ────
            try:
                cursor.execute("""
                    SELECT bo.id, bo.bot_id, bo.order_id, bo.client_order_id, bo.order_type, bo.amount, bo.status, bo.price, bo.step, bo.cycle_id, b.pair, b.config
                    FROM bot_orders bo
                    JOIN bots b ON bo.bot_id = b.id
                    WHERE bo.status IN ('new', 'open', 'partially_filled', 'placing', 'cancelling')
                      AND bo.order_id IS NOT NULL
                      AND bo.order_id != ''
                      AND bo.order_id NOT LIKE 'PENDING_%'
                      AND bo.created_at < ?
                      AND b.is_active = 1
                """, (int(ENGINE_START_TIME),))
                stale_rows = cursor.fetchall()
                if stale_rows:
                    logger.info(f"🔎 [STARTUP-STALE-CHECK] Found {len(stale_rows)} pending/new orders from previous session. Verifying against exchange...")
                    for row in stale_rows:
                        db_row_id, bot_id, order_id, client_order_id, order_type, amount, status, price, step, cycle_id, pair, config_json = row
                        try:
                            import json
                            from config.settings import config as app_config
                            from engine.exchange_interface import normalize_market_type
                            
                            cfg = json.loads(config_json) if config_json else {}
                            mt = normalize_market_type(cfg.get('market_type', app_config.MARKET_TYPE))
                            exchange = self.exchanges.get(mt)
                            if not exchange:
                                continue
                            
                            if order_id and any(str(order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                                continue

                            logger.info(f"🔎 [STARTUP-STALE-CHECK] Verifying {order_type} {order_id} ({pair})...")
                            order = exchange.fetch_order(order_id, pair)
                            if not order:
                                continue

                            # Always use the real exchange order_id from CCXT response
                            exchange_order_id = str(order.get('id') or order_id)

                            if order.get('status') == 'filled':
                                fill_price = float(order.get('average') or order.get('price') or 0)
                                fill_qty = float(order.get('filled') or 0)
                                fill_ts_ms = order.get('lastTradeTimestamp') or order.get('timestamp') or 0
                                fill_ts = int(fill_ts_ms // 1000) if fill_ts_ms else int(time.time())

                                if fill_qty <= 0 or fill_price <= 0:
                                    continue

                                from engine.ledger import credit_fill, seal_trade_state
                                logger.info(
                                    f"🚨 [STARTUP-STALE-RECOVERY] Bot {bot_id}: Order {exchange_order_id} confirmed filled on exchange. Crediting fill."
                                )
                                credited = credit_fill(
                                    bot_id=bot_id,
                                    order_id=exchange_order_id,
                                    cumulative_qty=fill_qty,
                                    avg_price=fill_price,
                                    order_type=order_type.lower(),
                                    is_cumulative=True,
                                    fill_ts=fill_ts
                                )
                                if credited:
                                    if order_type.upper() == 'TP':
                                        from engine.ledger import register_tp_cascade
                                        register_tp_cascade(bot_id, pair, fill_price, exit_fill_ts=fill_ts)
                                    seal_trade_state(bot_id)
                                    if order_type.lower() == 'grid':
                                        stats['grid_fills'] += 1
                                    elif order_type.lower() == 'entry':
                                        stats['entry_fills'] += 1
                                    elif order_type.lower() == 'tp':
                                        stats['tp_fills'] += 1
                            elif order.get('status') in ('canceled', 'cancelled', 'expired', 'rejected'):
                                from engine.database import update_order_status
                                logger.info(f"🚨 [STARTUP-STALE-RECOVERY] Bot {bot_id}: Order {exchange_order_id} confirmed canceled on exchange. Updating DB status.")
                                update_order_status(order_id, 'cancelled', bot_id, 0.0)
                        except Exception as ex_err:
                            logger.error(f"[STARTUP-STALE-CHECK] Failed verifying order {order_id} for bot {bot_id}: {ex_err}")
            except Exception as err:
                logger.error(f"[STARTUP-STALE-CHECK] Outer error: {err}")

            conn.commit()
            pass # conn.close() disabled for singleton safety

        stats['total'] = stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills']
        return stats





    def _mark_order_filled(self, cursor, order_id, fill_price):

        cursor.execute("""

            UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?

            WHERE order_id = ?

        """, (int(time.time()), fill_price, int(time.time()), order_id))



    def _handle_offline_tp_fill(self, cursor, bot_id, bot_name, fill_price, symbol, exchange=None):

        # GUARD: Never call _reset_bot_after_tp_internal with an invalid price (belt-and-suspenders)

        # The caller already guards, but this function is the final safety net.

        if not fill_price or float(fill_price) <= 0:

            logger.warning(

                f"⚠️ [OFFLINE-TP-GUARD] Skipping reset for Bot {bot_id} ({bot_name}): "

                f"fill_price={fill_price} is invalid (0 or None). Will not corrupt trade history."

            )

            return

        _reset_bot_after_tp_internal(cursor, bot_id, exit_price=fill_price, action_label='OFFLINE_TP', exchange=exchange)

        # Note: reset_bot_after_tp handles DB commits internaly, but here we are in a transaction from caller...

        # Ideally reset_bot_after_tp should be transaction-aware or we use a separate connection. 

        # Using a separate connection inside reset_bot_after_tp is fine as sqlite handles concurrency (WAL).



    def _handle_offline_grid_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry, symbol):

        """

        Adopts a physical grid fill into the trade state using absolute atomic logic.

        Uses force_step=True to ensure logical state matches physical reality.

        """

        from engine.database import log_trade, recompute_invested_from_orders, set_trade_from_ledger

        fill_cost = fill_price * fill_amount

        new_step = (current_step or 0) + 1

        

        # 🚀 ROOT CAUSE FIX: Recompute exact mathematical truth from bot_orders and SET (overwrite)

        # the trades table. We NO LONGER ADD to total_invested, preventing double-counts.

        true_cost, true_avg, true_qty, true_step = recompute_invested_from_orders(bot_id)

        if true_step < new_step: true_step = new_step

        set_trade_from_ledger(bot_id, true_cost, true_avg, true_step)

        

        log_trade(bot_id, 'OFFLINE_GRID', symbol, fill_price, fill_amount, fill_cost, f"GRID_{new_step}", new_step, "Offline Grid Fill", 0)

        logger.info(f"✅ [OFFLINE-ADOPTION] Force-aligned {bot_name} to Step {new_step} based on physical Grid footprint.")

        

        # (POST-FILL ANCHOR removed: Exact math via the receipt footprint is correct; don't blindly snap to physical size)



    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, symbol, timestamp_sec):

        """

        Adopts a physical entry fill into the trade state using atomic logic.

        Uses force_step=True to break any Step 0 deadlock.

        """

        from engine.database import log_trade, recompute_invested_from_orders, set_trade_from_ledger

        fill_cost = fill_price * fill_amount



        # 🚀 ROOT CAUSE FIX: Force Step 1 alignment for physical entries, replacing relative ADDs

        # with an absolute SET from the true ledger. Prevents WebSocket double-counting.

        true_cost, true_avg, true_qty, true_step = recompute_invested_from_orders(bot_id)

        if true_step < 1: true_step = 1

        set_trade_from_ledger(bot_id, true_cost, true_avg, true_step)

        

        log_trade(bot_id, 'OFFLINE_ENTRY', symbol, fill_price, fill_amount, fill_cost, "ENTRY", 1, "Offline Entry Fill", 0)

        logger.info(f"✅ [OFFLINE-ADOPTION] Force-aligned {bot_name} to Step 1 based on physical Entry footprint.")

        

        # (POST-FILL ANCHOR removed: Exact math via the receipt footprint is correct; don't blindly snap to physical size)



    # ------------------------------------------------------------------

    # STEP 2: BOT-CENTRIC VALIDATION

    # ------------------------------------------------------------------

    def validate_individual_bots(self, bot_states: List[BotState], all_orders: Dict[str, List[ExchangeOrder]]) -> List[ReconciliationResult]:

        """

        Ask each bot: "You say you are in trade. Do your orders exist on the exchange?"

        If not -> Zombie -> Reset.

        """

        from engine.database import get_connection as _gc

        _all_pairs = [r[0] for r in _gc().execute("SELECT DISTINCT pair FROM bots").fetchall()]

        

        results = []

        

        for bot in bot_states:

            # Common: Check propagation delay

            if (time.time() - bot.basket_start_time) < 60:

                continue



            pair_orders = all_orders.get(normalize_symbol(bot.pair), [])

            

            # -----------------------------------------------------------

            # CASE A: BOT IS IN TRADE (Zombie Check)

            # -----------------------------------------------------------

            if bot.in_trade:

                # Check if this bot's orders exist in the exchange list

                has_orders = False

                for order in pair_orders:

                    # Check Client Order ID (Best way)

                    if order.client_order_id and f"CQB_{bot.bot_id}_" in order.client_order_id:

                        has_orders = True

                        break

                    # Check stored Exchange IDs

                    if str(order.order_id) == str(bot.entry_order_id) or str(order.order_id) == str(bot.tp_order_id):

                        has_orders = True

                        break

                

                if not has_orders:

                    # 🔑 ORDER-ID LEDGER SELF-HEAL

                    # Before declaring zombie, recompute the ledger from confirmed order fills.

                    # If total_invested was a stale counter (bot has no confirmed fills this cycle),

                    # the fix zeroes it out and the bot is no longer "in trade" — not a zombie.

                    try:

                        from engine.database import sync_trades_from_orders as _sync_ledger

                        _corrected = _sync_ledger(bot.bot_id)

                        if _corrected:

                            # Re-read invested after correction

                            from engine.database import get_connection as _gc

                            _c = _gc()

                            _new_inv = (_c.execute(

                                "SELECT total_invested FROM trades WHERE bot_id=?", (bot.bot_id,)

                            ).fetchone() or (0,))[0]

                            if float(_new_inv or 0) <= 1e-6:

                                logger.info(

                                    f"✅ [LEDGER-HEAL] Bot {bot.name} (ID {bot.bot_id}): "

                                    f"total_invested corrected to 0 from order fills — not a zombie."

                                )

                                continue  # Skip zombie warning; bot is correctly IDLE

                    except Exception as _sh_err:

                        logger.warning(f"[LEDGER-HEAL] sync failed for bot {bot.bot_id}: {_sh_err}")



                    # --- PHANTOM BOT REPORT ---

                    # If we get here, bot still has invested > 0 after ledger sync —

                    # genuinely IN TRADE but no orders on exchange. Defer to Global Net Resolution.

                    # 🛡️ [RECON-HEDGE-CHILD-NO-TP] (v3.6.7)
                    # Give a targeted warning for hedge children specifically —
                    # the generic "IN TRADE but has NO orders" message doesn't
                    # distinguish between a standard zombie and a hedge child
                    # that lost its BE TP due to a handle_tp_completion race.
                    try:
                        from engine.database import get_connection as _gc_recon
                        _recon_conn = _gc_recon()
                        _hc_info = _recon_conn.execute(
                            "SELECT b.bot_type, t.open_qty, t.tp_order_id "
                            "FROM bots b LEFT JOIN trades t ON t.bot_id = b.id "
                            "WHERE b.id = ?",
                            (bot.bot_id,)
                        ).fetchone()
                        if _hc_info and _hc_info[0] == 'hedge_child':
                            _hc_open_qty = float(_hc_info[1] or 0)
                            _hc_tp_id = _hc_info[2]
                            if _hc_open_qty > 0.0001 and not _hc_tp_id:
                                logger.warning(
                                    f"⚠️ [RECON-HEDGE-CHILD-NO-TP] Bot {bot.bot_id} ({bot.name}) "
                                    f"is a hedge_child IN TRADE with open_qty={_hc_open_qty:.6f} "
                                    f"but tp_order_id is NULL and no live TP order found. "
                                    f"handle_tp_completion may have failed silently — "
                                    f"check logs for [HANDLE-TP-COMPLETION] warnings. "
                                    f"maintain_orders fallback [HEDGE-BE-FALLBACK] will self-heal on next cycle."
                                )
                    except Exception as _hc_recon_err:
                        logger.debug(f"[RECON-HEDGE-CHILD-NO-TP] check failed (non-fatal): {_hc_recon_err}")

                    logger.warning(f"⚠️ [RECON] Bot {bot.name} (ID {bot.bot_id}) is IN TRADE but has NO orders. Deferring to Global Net Resolution.")










            # -----------------------------------------------------------

            # CASE B: BOT IS IDLE (Orphan Order Check)

            # -----------------------------------------------------------

            else:

                # Bot is IDLE/Scanning. It should have NO open orders.

                my_orders = [o for o in pair_orders if (o.client_order_id and f"CQB_{bot.bot_id}_" in o.client_order_id)]

                

                if my_orders:

                    # ORPHAN DETECTED

                    # 🚀 SIGNATURE-BASED ACCURACY FIX:

                    # An IDLE bot with orders is a discrepancy, but we should NOT force-cancel

                    # if they have the system's DNA (CQB prefix). 

                    # Instead, we report them as requiring intervention.

                    logger.warning(f"⚠️ ORPHAN DETECTED: Bot {bot.name} (ID {bot.bot_id}) is IDLE but has {len(my_orders)} open CQB orders. PRESERVING for forensic link.")

                    

                    res = ReconciliationResult(

                        bot_id=bot.bot_id,

                        bot_name=bot.name,

                        pair=bot.pair,

                        action_taken=ReconciliationAction.ROGUE_POSITION, # Use Rogue to trigger Wizard

                        details=f"Orphan Bot Orders Found ({len(my_orders)}) - Manual Link Suggested",

                        requires_manual_intervention=True

                    )

                    results.append(res)

                    

                    # 🗑️ AUTO-CLEAN: The bot is IDLE. Any hanging limit orders are dangerous orphans 

                    # that will block MARKET-FLATTEN reduceOnly operations.

                    logger.warning(f"🧹 [ORPHAN-CLEAN] Cancelling {len(my_orders)} physical orphans for IDLE bot {bot.name}.")

                    for o in my_orders:

                        try:

                            ex_heal = self.exchanges.get('future')

                            if ex_heal:

                                pair_normalized = normalize_symbol(bot.pair)

                                _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                                ex_heal.cancel_order(o.order_id, _ccxt_pair)

                        except Exception as e:

                            logger.error(f"Failed to cancel orphan order {o.order_id}: {e}")

                    

                    # Mark them closed in DB to stop the cycle

                    conn = get_connection()

                    for o in my_orders:

                        conn.execute("UPDATE bot_orders SET status='cancelled', updated_at=? WHERE order_id=?", (int(time.time()), o.order_id))

                    conn.commit()

        

        return results



    # ------------------------------------------------------------------

    # STEP 3: NET-SUM VERIFICATION

    # ------------------------------------------------------------------

    def _get_dust_thresholds(self, pair: str) -> Tuple[float, float]:

        """

        Fetches live minNotional and minQty for a pair from the exchange metadata.

        Returns (min_notional, min_qty). Defaults to (5.0, 0.0) if fetch fails.

        """

        try:

            ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)

            if not ex: return 5.0, 0.0

            market = ex.exchange.market(pair)

            min_notional = float(market.get('limits', {}).get('cost', {}).get('min', 5.0) or 5.0)

            min_qty = float(market.get('limits', {}).get('amount', {}).get('min', 0.0) or 0.0)

            return min_notional, min_qty

        except Exception as e:

            logger.warning(f"⚠️ Failed to fetch market limits for {pair}: {e}. Using $5.0 fallback.")

            return 5.0, 0.0



    def resolve_net_mismatch(self, bot_states: List[BotState], positions: Dict[str, List[ExchangePosition]], all_orders: Dict[str, List[ExchangeOrder]] = {}, force_adoption: bool = False) -> List[ReconciliationResult]:

        """

        Calculates Net Virtual Position and compares with Net Physical Position.

        If significant deviation exists, identifies 'Ghost' bots and resets them using SYSTEM_FIX.

        """

        from engine.database import get_connection as _gc

        import config as _cfg

        _is_testnet = getattr(_cfg, 'TESTNET', False) or getattr(_cfg, 'DEMO_TRADING', False)

        _all_pairs = [r[0] for r in _gc().execute("SELECT DISTINCT pair FROM bots").fetchall()]

        

        results = []

        

        # Group bots by pair (normalized)

        bots_by_pair = {}

        for bot in bot_states:

            norm_pair = normalize_symbol(bot.pair)

            if norm_pair not in bots_by_pair: bots_by_pair[norm_pair] = []

            bots_by_pair[norm_pair].append(bot)

            

        

        # FUNDAMENTAL FIX: Normalize the incoming CCXT positions dictionary keys

        # The exchange keys are like "BTC/USDC" but we must process them as "BTCUSDC"

        normalized_positions = {}

        for raw_pair, pos_list in positions.items():

            norm_p = normalize_symbol(raw_pair)

            normalized_positions[norm_p] = pos_list

            

        # FUNDAMENTAL FIX: Include pairs that have positions but no bots (Rogue Positions)

        # We must ensure that EVERY pair in normalized_positions is a key in bots_by_pair

        all_exchange_pairs = list(normalized_positions.keys())

        for norm_p in all_exchange_pairs:

            if norm_p not in bots_by_pair:

                bots_by_pair[norm_p] = [] # Empty list of bots for this pair



        # 🚀 EXACT MATH PRE-FETCH

        # A professional system has 0 drift. Avoid float division! Request true ledger qty exactly!

        from .database import get_connection

        bot_qtys = {}

        bot_pending = {} # Track open orders that might account for gaps

        conn = get_connection()

        try:

            cursor = conn.cursor()

            for bot in bot_states:

                if bot.in_trade or True: # Check all bots to be safe

                    cursor.execute("""

                        SELECT 

                            COALESCE(SUM(

                                CASE 

                                    WHEN status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled') OR (status IN ('canceled', 'cancelled') AND filled_amount > 0) THEN

                                        CASE 

                                            -- Entry-side order types that ADD to the bot's held qty

                                            WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount

                                            -- Exit-side order types that REDUCE the bot's held qty

                                            -- NOTE: 'hedge' is intentionally excluded here.

                                            -- Hedge orders open a SHORT on the exchange but do NOT

                                            -- cancel out the LONG entry fills the bot recorded.

                                            -- The hedge-aware correction block below (lines ~1688-1715)

                                            -- handles hedge contribution to virtual_net_qty separately.

                                            -- Including 'hedge' here causes double-subtraction.

                                            WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl', 'flatten_close') THEN -filled_amount

                                            ELSE 0.0

                                        END

                                    ELSE 0.0

                                END

                            ), 0.0) as filled_qty,

                            COALESCE(SUM(

                                CASE 

                                    WHEN status IN ('new', 'open', 'placing', 'cancelling') THEN

                                        CASE 

                                            WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN (amount - filled_amount)

                                            ELSE 0.0

                                        END

                                    ELSE 0.0

                                END

                            ), 0.0) as pending_add_qty,

                            COALESCE(SUM(

                                CASE 

                                    WHEN status IN ('new', 'open', 'placing', 'cancelling') THEN

                                        CASE 

                                            -- Open TP/close orders waiting to fill

                                            WHEN order_type IN ('tp', 'close', 'dust_close', 'sl', 'adoption_reduce', 'flatten_close') THEN (amount - filled_amount)

                                            ELSE 0.0

                                        END

                                    ELSE 0.0

                                END

                            ), 0.0) as pending_reduce_qty

                        FROM bot_orders 

                        WHERE bot_id = ? AND status NOT IN ('reset_cleared', 'auto_closed', 'rejected')

                        AND (cycle_id = (SELECT cycle_id FROM trades WHERE bot_id = ?) OR cycle_id IS NULL)

                    """, (bot.bot_id, bot.bot_id))

                    row = cursor.fetchone()

                    if row:

                        bot_qtys[bot.bot_id] = float(row[0])

                        bot_pending[bot.bot_id] = {

                            'add': float(row[1]),

                            'reduce': float(row[2])

                        }

                    else:

                        bot_qtys[bot.bot_id] = 0.0

                        bot_pending[bot.bot_id] = {'add': 0.0, 'reduce': 0.0}



        finally:

            pass # conn.close() disabled for singleton safety



        for pair, bots in bots_by_pair.items():

            # 'pair' here is normalized

            # 1. Calc Virtual GROSS per direction (not signed net)

            # In One-Way mode, multiple bots may trade the same pair in the same direction.

            # NET comparison (LONG−SHORT) will ALWAYS fire when >1 same-direction bot is active.

            # GROSS per-direction comparison is the correct approach: compare all LONG virtual qty

            # vs all physical LONG qty, and all SHORT virtual qty vs all physical SHORT qty, independently.

            virtual_long_qty = 0.0

            virtual_long_usd = 0.0

            virtual_short_qty = 0.0

            virtual_short_usd = 0.0

            

            for b in bots:

                # v2.3.4: Professional Cent-Level Parity.

                # Include ANY bot that holds a ledger quantity (even if Scanning).

                # This ensures residues are correctly identified vs the exchange.

                qty = bot_qtys.get(b.bot_id, 0.0)

                if (b.in_trade or qty > 1e-8) and b.avg_entry_price > 0:

                    qty = bot_qtys.get(b.bot_id, 0.0)

                    if b.direction.upper() == 'LONG':

                        virtual_long_qty += qty

                        virtual_long_usd += b.total_invested

                    else:

                        virtual_short_qty += qty

                        virtual_short_usd += b.total_invested



            # Keep legacy virtual_net for backward compat with ghost-detection code below

            virtual_net = virtual_long_qty - virtual_short_qty

            virtual_net_usd = virtual_long_usd - virtual_short_usd

            total_virtual_invested = virtual_long_usd + virtual_short_usd

            gross_virtual_qty = virtual_long_qty + virtual_short_qty



            # 2. Get Physical GROSS per direction from exchange

            pair_normalized = normalize_symbol(pair)

            pair_positions = normalized_positions.get(pair_normalized, [])

            

            physical_long_qty = 0.0

            physical_long_usd = 0.0

            physical_short_qty = 0.0

            physical_short_usd = 0.0

            total_physical_notional = 0.0

            physical_net = 0.0

            physical_net_usd = 0.0

            rep_side = "N/A"



            for p in pair_positions:

                val = abs(p.size) * p.entry_price

                total_physical_notional += val

                rep_side = p.side

                if p.side == 'LONG':

                    physical_long_qty += abs(p.size)

                    physical_long_usd += val

                    physical_net += abs(p.size)

                    physical_net_usd += val

                else:

                    physical_short_qty += abs(p.size)

                    physical_short_usd += val

                    physical_net -= abs(p.size)

                    physical_net_usd -= val



            # 3. GROSS per-direction gap (the real check)

            long_gap_qty = virtual_long_qty - physical_long_qty

            long_gap_usd = virtual_long_usd - physical_long_usd

            short_gap_qty = virtual_short_qty - physical_short_qty

            short_gap_usd = virtual_short_usd - physical_short_usd



            logger.info(

                f"⚖️ RECON AUDIT [{pair_normalized}]: "

                f"Virtual LONG ${virtual_long_usd:.2f} vs Physical LONG ${physical_long_usd:.2f} | "

                f"Virtual SHORT ${virtual_short_usd:.2f} vs Physical SHORT ${physical_short_usd:.2f}"

            )







            # --- v2.5.0 GLOBAL NETTING MASTER GUARD ---

            # Before auditing individual bots, check if the entire pair is in parity.

            # In One-Way mode, bot-level discrepancies are often just netting artifacts.

            # If the Global Net (Bots + Hedges) matches Exchange Net, skip aggressive wipes.

            from .database import get_pair_virtual_net

            global_system_net = get_pair_virtual_net(pair_normalized)

            

            # Signed physical net (positive=LONG, negative=SHORT)

            physical_long = sum(p.size for p in pair_positions if p.side == 'LONG')

            physical_short = sum(p.size for p in pair_positions if p.side == 'SHORT')

            global_physical_net = physical_long - physical_short

            

            global_diff = abs(global_system_net - global_physical_net)

            if global_diff < 0.0001:

                logger.info(f"🛡️ [GLOBAL-NET-OK] {pair_normalized}: System Net {global_system_net:.6f} matches Exchange {global_physical_net:.6f}. Individual bot audits will bypass aggressive wipes.")

                global_net_ok = True

            else:

                logger.warning(f"⚠️ [GLOBAL-NET-MISMATCH] {pair_normalized}: System {global_system_net:.6f} vs Exchange {global_physical_net:.6f} (Diff={global_diff:.6f}). Auditing bots...")

                global_net_ok = False

                

                # 🚀 V3.3.1: WIPE AUDIT TRAP

                try:

                    from .reconciler_wipe_audit import audit_bot_wipes

                    with get_connection() as conn:

                        cur = conn.cursor()

                        for bot_obj in bots:

                            audit = audit_bot_wipes(cur, bot_obj.bot_id, pair_normalized, global_diff)

                            if audit.probable_cause_match:

                                logger.error(f"💎 [WIPE-MATCH] Identified suspect unproved wipes on Bot {bot_obj.bot_id} totaling {audit.total_suspect_qty:.4f}. This matches the system mismatch of {global_diff:.4f}!")

                except Exception as e:

                    logger.error(f"Error during wipe audit: {e}")



            # 🚀 Case A: Impossible Bot Mass Detection (Vanished or Ghost Bots)

            for b in bots:

                # v2.3.4: Ghost detection must see residues in Scanning bots.

                bot_qty = bot_qtys.get(b.bot_id, 0.0)

                if b.in_trade or bot_qty > 1e-8:

                    bot_qty = bot_qtys.get(b.bot_id, 0.0)

                    

                    opposite_virtual_qty = sum(

                        bot_qtys.get(other_b.bot_id, 0.0)

                        for other_b in bots

                        if other_b.in_trade and other_b.avg_entry_price > 0 and other_b.direction.upper() != b.direction.upper()

                    )

                    

                    physical_matching_direction_qty = sum(

                        abs(p.size) for p in pair_positions if p.side == b.direction.upper()

                    )

                    physical_opposite_direction_qty = sum(

                        abs(p.size) for p in pair_positions if p.side != b.direction.upper()

                    )

                    

                    # 🚀 UNIVERSAL BOUND EQUATION (Hedge & One-Way Compatible)

                    # If this side exists physically, the bot can claim up to (Physical + Opposing Virtual).

                    # If this side DOES NOT exist physically, it can claim AT MOST (Opposing Virtual - Physical Opposing).

                    if physical_matching_direction_qty > 0:

                        max_possible_qty = physical_matching_direction_qty + opposite_virtual_qty

                    else:

                        max_possible_qty = max(0.0, opposite_virtual_qty - physical_opposite_direction_qty)

                    

                    # --- v2.4.1 INTER-BOT VIRTUAL NETTING (ONE-WAY MODE RESIDUE) ---

                    # Professional resolution for "Wrong-Side" residue in One-Way Mode.

                    # If a bot holds residue on the opposite side of the physical exchange position,

                    # it is mathematically impossible to trade it out. Consolidate via virtual entry.

                    is_hedged = (b.hedge_child_bot_id is not None) or (b.parent_bot_id is not None)

                    if len(pair_positions) <= 1 and not is_hedged:

                        # Only target bots on the OPPOSITE side of the physical reality

                        if physical_matching_direction_qty < 0.0001 and bot_qty > 0.0001:

                            cycle_phase = getattr(b, 'cycle_phase', 'IDLE')

                            is_finished = (b.in_trade is False) or (cycle_phase in ('IDLE', 'CARRY_PENDING'))

                            

                            # v2.4.1: If on wrong side of a physical position, it is a Ghost/Zombie 

                            # and is eligible for consolidation even if 'ACTIVE'.

                            is_wrong_side = (physical_opposite_direction_qty > 0.0001)

                            

                            if (is_finished or is_wrong_side) and (b.total_invested != 0 or bot_qty > 0):

                                # Threshold check: Only net if it's below min_notional (avoiding accidental wipes of large errors)

                                # --- v2.5.0 FORENSIC PARITY PROTOCOL ---

                                # We NO LONGER wipe "Wrong-Side" residue. 

                                # We must find the physical trade ID that caused it.

                                logger.info(f"🔍 [FORENSIC-TRIGGER] {b.name}: Persistent residue detected. Triggering deep history scan.")

                                from engine.parity_gates import forensic_adopt_allowed
                                if forensic_adopt_allowed():
                                    self.reconstruct_offline_fills(pair_filter=b.pair, forensic_mode=True)
                                else:
                                    logger.warning(
                                        f"🚨 [FORENSIC-DISABLED] {b.name}: residue on {b.pair} — "
                                        f"set REQUIRE_MANUAL_PROOF; use trade-history proof or UI proof flatten."
                                    )
                                    from engine.database import flag_pair_ledger_mismatch, audit_pair_ledger_vs_exchange
                                    ex_audit = (
                                        self.exchanges.get('future')
                                        if getattr(self, 'exchanges', None)
                                        else getattr(self, 'exchange', None)
                                    )
                                    if ex_audit:
                                        mm = audit_pair_ledger_vs_exchange(ex_audit)
                                        if mm:
                                            flag_pair_ledger_mismatch(mm)

                                if global_net_ok:
                                    continue

                    

                    # Exact precision enforced.
                    logger.warning(f"DEBUG: b.name={b.name}, bot_qty={bot_qty}, max_possible_qty={max_possible_qty}, cond={bot_qty > (max_possible_qty + 0.0001)}")
                    if bot_qty > (max_possible_qty + 0.0001):

                        if global_net_ok:

                             logger.info(f"🛡️ [BYPASS-WIPE] {b.name}: Individual ghost detected ({bot_qty:.6f} > {max_possible_qty:.6f}), but Global Net is BALANCED. Bypassing wipe — this is a netting artifact.")

                             continue



                        logger.critical(f"👻 SYSTEM MISMATCH on {pair}: Bot {b.name} claims {bot_qty:.6f} QTY, but Math Capacity is only {max_possible_qty:.6f} QTY.")

                        

                        proof = self._find_proof_of_exit(b)

                        if proof:

                            logger.info(f"✅ PROOF FOUND: Bot {b.name} exited via order {proof.get('id')} ({proof.get('clientOrderId')}). Resetting safely.")

                            from .database import log_reconciliation

                            log_reconciliation(

                                bot_id=b.bot_id,

                                pair=b.pair,

                                action="RESET_WITH_PROOF",

                                details=f"Found exit fill {proof.get('id')} in exchange history.",

                                proof_order_id=proof.get('id')

                            )

                            res = ReconciliationResult(

                                bot_id=b.bot_id,

                                bot_name=b.name,

                                pair=b.pair,

                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                details=f"Reconciled: Exit proof found in history ({proof.get('clientOrderId')}).",

                                requires_manual_intervention=False

                            )

                            results.append(res)

                            self._fix_ghost_bot(b, proof_order_id=proof.get('id'))

                        else:

                            is_valid_entry = b.has_confirmed_entry

                            if not is_valid_entry:

                                entry_proof = self._verify_entry_existence(b)

                                if not entry_proof:

                                    logger.info(f"✅ INVALID ENTRY DETECTED: Bot {b.name} Entry Order {b.entry_order_id} not found/filled on exchange. Resetting Phantom State.")

                                    from .database import log_reconciliation

                                    log_reconciliation(

                                        bot_id=b.bot_id, pair=b.pair, action="RESET_PHANTOM_ENTRY",

                                        details="Entry order not found or not filled on exchange. Bot state was phantom."

                                    )

                                    res = ReconciliationResult(

                                        bot_id=b.bot_id, bot_name=b.name, pair=b.pair,

                                        action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                        details=f"Reset Phantom Entry (Order {b.entry_order_id} invalid)", requires_manual_intervention=False

                                    )

                                    results.append(res)

                                    self._fix_ghost_bot(b, proof_order_id="PHANTOM_ENTRY")

                                    continue

                                else:

                                    is_valid_entry = True



                            if is_valid_entry:
                                is_unauthorized_loss = False
                                max_claimable_qty = 0.0
                                
                                is_hedged = (b.hedge_child_bot_id is not None) or (b.parent_bot_id is not None)
                                
                                if is_hedged:
                                    # Hedge-aware pair net check
                                    p_id = b.bot_id if b.hedge_child_bot_id is not None else b.parent_bot_id
                                    c_id = b.hedge_child_bot_id if b.hedge_child_bot_id is not None else b.bot_id
                                    
                                    p_state = next((x for x in bots if x.bot_id == p_id), None)
                                    if p_state:
                                        p_dir = p_state.direction.upper()
                                    else:
                                        # Fallback to database
                                        from engine.database import get_connection as _gc
                                        with _gc() as conn:
                                            dir_row = conn.execute("SELECT direction FROM bots WHERE id=?", (p_id,)).fetchone()
                                            p_dir = str(dir_row[0] or 'LONG').upper() if dir_row else 'LONG'
                                            
                                    parent_qty = bot_qtys.get(p_id, 0.0)
                                    child_qty = bot_qtys.get(c_id, 0.0)
                                    
                                    if p_dir == 'LONG':
                                        pair_net_virtual = parent_qty - child_qty
                                    else:
                                        pair_net_virtual = -parent_qty + child_qty
                                        
                                    pair_net_physical = global_physical_net
                                    
                                    from engine.parity_gates import qty_tolerance
                                    tol = qty_tolerance()
                                    
                                    if abs(pair_net_virtual - pair_net_physical) <= tol:
                                        # Pair is balanced — skip UNAUTHORIZED_LOSS entirely
                                        is_unauthorized_loss = False
                                    else:
                                        # Only if pair net is NOT balanced, check individual bot contribution using physical as ground truth
                                        max_claimable_qty = physical_matching_direction_qty
                                        is_unauthorized_loss = bot_qty > (physical_matching_direction_qty + 0.0001)
                                else:
                                    # Non-hedged bot: use unrelated_opposite_virtual formula
                                    # Since there's no hedge child, unrelated_opposite_virtual is same as opposite_virtual_qty
                                    unrelated_opposite_virtual = opposite_virtual_qty
                                    max_claimable_qty = physical_opposite_direction_qty - unrelated_opposite_virtual
                                    if max_claimable_qty < 0.0:
                                        max_claimable_qty = 0.0
                                    is_unauthorized_loss = (physical_matching_direction_qty < 0.0001) and (bot_qty > max_claimable_qty + 0.0001)

                                if is_unauthorized_loss:
                                    logger.critical(
                                        f"💥 [UNAUTHORIZED POSITION LOSS] Bot {b.name}: Claims {bot_qty:.6f} units but Math Capacity is {max_claimable_qty:.6f}! "
                                        f"Entry was confirmed, so the position vanished externally (Liquidated/ADL/Manual). "
                                        f"Strict ID-Based Tracking Doctrine enforces MANUAL INTERVENTION. No auto-wipe."
                                    )
                                    from .database import log_reconciliation
                                    log_reconciliation(
                                        bot_id=b.bot_id,
                                        pair=b.pair,
                                        action="UNAUTHORIZED_LOSS",
                                        details=f"Missing Mass: Bot claims {bot_qty:.6f}, Math Capacity is {max_claimable_qty:.6f}. Manual intervention required."
                                    )
                                    results.append(ReconciliationResult(
                                        bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                        action_taken=ReconciliationAction.REQUIRE_MANUAL,
                                        details="Unauthorized physical position loss detected. Manual intervention required.", 
                                        requires_manual_intervention=True
                                    ))
                                else:
                                    logger.warning(
                                        f"⚠️ [PARTIAL VANISH DEFERRED] Bot {b.name}: Claims {bot_qty:.6f} units but Physical is {physical_matching_direction_qty:.6f} (max_capacity={max_claimable_qty:.6f}). "
                                        f"Since Physical > 0, we will NOT wipe the bot's memory. Deferring to Net Gap Resolution."
                                    )

            # --- CASE B.2: STRUCTURAL GHOST DETECTION ---

            # Even if net-notional is close, if a bot claims high step but lacks history, it's a structural ghost (legacy state).

            for b in bots:

                # 🚀 ARCHITECTURAL FIX: 

                # Do not execute Structural Ghost destruction if the bot's entry is explicitly confirmed. 

                # 'entry_confirmed=1' indicates the position was successfully spawned via WebSocket fill, 

                # or was manually adopted/audited by the user/system.

                # Use cycle_start_time (Absolute Age) for immunity checks.

                # Immunity active if the trade cycle started within last 15s.

                trade_age = int(time.time()) - (b.cycle_start_time if b.cycle_start_time > 0 else b.basket_start_time)

                if trade_age < 15:

                    logger.info(f"⏳ [RECON-GRACE] Bot {b.name}: Recently started ({trade_age}s ago). Immunity active.")

                    continue



                if b.in_trade and b.total_invested > 1.0 and not b.has_confirmed_entry and b.current_step > 0:

                    # Check if entry order is still open (mid-fill protection)

                    entry_is_active = False

                    if b.entry_order_id:

                        pair_orders = all_orders.get(normalize_symbol(b.pair), [])

                        entry_order_ids = {str(o.order_id) for o in pair_orders}

                        if str(b.entry_order_id) in entry_order_ids:

                            logger.info(f"🔒 [MID-FILL GUARD] Bot {b.name}: Entry order {b.entry_order_id} still open on exchange. Skipping ghost check — bot is actively filling.")

                            entry_is_active = True

                    if entry_is_active:

                        continue  # Skip ghost check entirely for this bot

                    try:

                        from .database import get_connection

                        conn = get_connection()

                        cursor = conn.cursor()



                        # 🛡️ PHYSICAL POSITION GUARD (ROOT CAUSE FIX):

                        # Before wiping ANY bot, verify the exchange shows near-zero physical inventory.

                        # If active_positions has a significant qty, this is NOT a ghost — the DB state

                        # is stale (e.g. basket_start_time=0 after CARRY) but the money is real.

                        # Wiping here would create the exact "6487 SUI gap never accounted for" bug.

                        norm_pair = b.pair.split(':')[0].replace('/', '')

                        phys_snap = cursor.execute(

                            "SELECT ABS(size) FROM active_positions WHERE pair=? AND side=?",

                            (norm_pair, 'LONG' if b.direction.upper() == 'LONG' else 'SHORT')

                        ).fetchone()

                        phys_qty = float(phys_snap[0]) if phys_snap and phys_snap[0] else 0.0



                        # QTY_EPSILON: 0.0001 units — below this is noise, above is a real position.

                        QTY_EPSILON = 0.0001

                        if phys_qty > QTY_EPSILON:

                            # Exchange has a real position — this is NOT a ghost. Do NOT wipe.

                            logger.warning(

                                f"🛡️ [STRUCTURAL-GHOST BLOCKED] Bot {b.name} was flagged as ghost "

                                f"(no cycle fills since basket_start={b.basket_start_time}), "

                                f"BUT exchange shows {phys_qty:.6f} physical units. REFUSING WIPE. "

                                f"Autonomous recovery will preserve this position."

                            )

                        else:

                            # 🚀 FIX (v2.6): Use cycle_id for Ghost detection.

                            # All fills belonging to the current session share the same cycle_id.

                            # The volatile basket_start_time (EE timer) resets on DCA hits,

                            # so we must NOT use it as a temporal filter for proof-of-mass.

                            cursor.execute("""

                                SELECT COUNT(*), SUM(amount * price) FROM bot_orders 

                                WHERE bot_id=? AND (status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled') OR (status IN ('canceled', 'cancelled') AND filled_amount > 0)) 

                                AND cycle_id = ?

                            """, (b.bot_id, b.cycle_id))

                            row = cursor.fetchone()

                            

                            # If bot has money invested, but ZERO filled orders in the current cycle

                            if not row or row[0] == 0:

                                logger.critical(f"👻 [STRUCTURAL-GHOST] Bot {b.name} claims ${b.total_invested:.2f} (Step {b.current_step}) but has NO filled orders in current cycle {b.cycle_id} AND exchange shows 0 physical. Resetting to truth.")

                                # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() — will block if CARRY_PENDING or physical > 0

                                wiped = safe_wipe_bot(

                                    b.bot_id, b.pair, b.direction,

                                    reason=f"STRUCTURAL_GHOST: ${b.total_invested:.2f} claimed, 0 cycle fills, 0 physical",

                                    exit_price=0.0, bypass_ledger_guard=True, human_approved=True, cursor=cursor

                                )

                                if wiped:

                                    from .database import log_reconciliation

                                    log_reconciliation(

                                        bot_id=b.bot_id, pair=b.pair, action="RESET_STRUCTURAL_GHOST",

                                        details=f"Structural Ghost: Claimed ${b.total_invested:.2f} but 0 Session Fills and 0 physical."

                                    )

                                    results.append(ReconciliationResult(

                                        bot_id=b.bot_id, bot_name=b.name, pair=b.pair,

                                        action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                        details="Reset Structural Ghost (No verifiable history, 0 physical)", requires_manual_intervention=False

                                    ))

                    except Exception as e:

                        logger.error(f"Error checking structural ghost for {b.name}: {e}")

                    finally:

                        if 'conn' in locals(): pass # conn.close() disabled for singleton safety



            # --- CASE C: SIGNIFICANT NOTIONAL DEVIATION (AUTO-REPAIR) ---

            # 🚀 FIX: Compare QTY directly, not USD notional.

            # Compute net QTY for each side (virtual from trades, physical from exchange positions).

            virtual_net_qty = 0.0

            for b in bots:

                # Include all bots with total_invested > 0 (includes CARRY_PENDING).

                if b.total_invested > 0 and b.avg_entry_price > 0:

                    qty = b.total_invested / b.avg_entry_price

                    virtual_net_qty += qty if b.direction.upper() == 'LONG' else -qty




            physical_net_qty = 0.0

            for p in pair_positions:

                physical_net_qty += abs(p.size) if p.side == 'LONG' else -abs(p.size)

            

            # 🛡️ ARCHITECT'S SHIELD: Subtract manual whitelists to ignore personal trades

            whitelists = get_manual_whitelists(pair)

            for w in whitelists:

                w_qty = float(w['qty'])

                physical_net_qty -= w_qty if w['side'] == 'LONG' else -w_qty

                logger.debug(f"🛡️ [RECONCILER] Subtracted whitelist for {pair} {w['side']}: {w_qty}")



            # Compute net USD for logging and side-mismatch checks

            virtual_net_usd = sum(b.total_invested if b.direction.upper() == 'LONG' else -b.total_invested for b in bots)

            physical_net_usd = sum(abs(p.size) * p.entry_price if p.side == 'LONG' else -abs(p.size) * p.entry_price for p in pair_positions)

            is_sole = (len(bots) == 1)

            

            delta_qty = abs(virtual_net_qty - physical_net_qty)

            # Keep delta_notional for logging only

            delta_notional = abs(virtual_net_usd - physical_net_usd)



            # 🚀 PRECISION UPGRADE: 1e-8 for QTY is the exchange standard.

            QTY_EPSILON = 1e-8



            # 🧹 STATE ENFORCEMENT & DUST CHASER

            # Must run BEFORE Virtual Consensus Guard to ensure perfectly matched dust is still wiped!

            dust_cleared_any = False

            for b in bots:

                # 🧹 PROFESSIONAL DUST CHASER (V2.3.0 Ledger-Adoption Architecture)

                # Solves the "Trapped Multi-Bot Dust" API rejection problem.

                if b.total_invested > 0 and b.total_invested < 5.0:

                    logger.warning(f"🧹 [DUST-CHASER] Bot {b.name} ({b.pair}): Holding only ${b.total_invested:.2f}. Evaluating clearance architecture.")

                    

                    total_physical_notional = abs(physical_net_usd)

                    

                    if total_physical_notional < 5.0:

                        # -------------------------------------------------------------

                        # SCENARIO A: Total Pair Notional < $5

                        # The entire pair is dust. We can safely flatten the exchange.

                        # -------------------------------------------------------------

                        logger.info(f"🧹 [DUST-CHASER] Scenario A: Total pair physical notional is ${total_physical_notional:.2f} (<$5). Flattening exchange.")

                        ex = self.exchanges.get('future')

                        if not ex and self.exchanges:

                            ex = list(self.exchanges.values())[0]

                            

                        dust_cleared = True

                        if ex and abs(physical_net_qty) > 1e-8:

                            exit_side = 'buy' if physical_net_qty < 0 else 'sell'

                            pos_side = 'SHORT' if physical_net_qty < 0 else 'LONG'

                            try:

                                logger.info(f"🧹 [DUST-CHASER] Executing PHYSICAL MARKET {exit_side.upper()} order for {abs(physical_net_qty):.6f} {b.pair}.")

                                _dc_audit_conn = get_connection()

                                _dc_audit_cur = _dc_audit_conn.cursor()

                                ex.create_order(

                                    symbol=b.pair, type='market', side=exit_side, amount=abs(physical_net_qty),

                                    params=_safe_order_params({'reduceOnly': True}, _is_testnet),

                                    emergency=True,

                                    _audit_cursor=_dc_audit_cur,

                                    _call_site="resolve_net_mismatch._dust_chaser_scenario_a:1770",

                                )

                                _dc_audit_conn.commit()

                                logger.info(f"✅ [DUST-CHASER] Physical exchange clearance successful for {b.pair}.")

                                # Reset physical values so subsequent bots see it as flat

                                physical_net_qty = 0.0

                                physical_net_usd = 0.0

                            except Exception as dust_err:

                                logger.error(f"❌ [DUST-CHASER] Failed to physically clear exchange dust for {b.pair}: {dust_err}")

                                dust_cleared = False

                        else:

                            dust_cleared = True # Exchange already 0

                        

                        if dust_cleared:

                            # Wipe all bots on this pair that hold dust

                            active_bots = [x for x in bots if x.total_invested > 0]

                            for d_bot in active_bots:

                                dust_qty = (d_bot.total_invested / d_bot.avg_entry_price) if d_bot.avg_entry_price > 0 else 0.0

                                if dust_qty > 0:

                                    save_bot_order(

                                        bot_id=d_bot.bot_id, order_type='dust_close', exchange_order_id=f"DUST_WIPE_{d_bot.bot_id}_{int(time.time())}",

                                        price=d_bot.avg_entry_price, amount=dust_qty, step=0, status='filled', client_order_id=f"CQB_{d_bot.bot_id}_DUSTWIPE"

                                    )

                                safe_wipe_bot(d_bot.bot_id, d_bot.pair, d_bot.direction, reason="DUST_CHASER: Scenario A - Total Pair Wipe", exit_price=0.0, bypass_ledger_guard=True, human_approved=True, cursor=cursor)

                                results.append(ReconciliationResult(

                                    bot_id=d_bot.bot_id, bot_name=d_bot.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                    details=f"Dust Chaser: Scrapped ${d_bot.total_invested:.2f} via Scenario A total physical wipe.", requires_manual_intervention=False

                                ))

                            dust_cleared_any = True

                            break # Wiped all bots on this pair, exit loop early

                    else:
                        # SCENARIO B: Pair physical >= $5 (multi-bot environment)
                        # The dust position is small but the exchange still holds it.
                        # We cannot virtual-wipe without closing the exchange position first.
                        # Place a GTC limit close order at current price and set cycle_phase
                        # to PARTIAL_CLOSE_PENDING. The dust position will close naturally.
                        logger.warning(
                            f"🧹 [DUST-CHASER] Scenario B: Bot {b.name} has dust position "
                            f"${b.total_invested:.2f} in multi-bot environment. "
                            f"Placing GTC close order instead of virtual wipe."
                        )
                        try:
                            exchange = self.exchanges.get('future')
                            if not exchange and self.exchanges:
                                exchange = list(self.exchanges.values())[0]
                            current_price = exchange.get_last_price(b.pair) if exchange else 0.0
                            
                            close_side = 'sell' if b.direction.upper() == 'LONG' else 'buy'
                            dust_qty = round(b.total_invested / b.avg_entry_price, 8) if b.avg_entry_price > 0 else 0.0
                            if dust_qty > 0.0001:
                                close_cid = f"CQB_{b.bot_id}_DUST_CLOSE_{int(time.time())}"
                                close_order = exchange.create_order(
                                    b.pair, 'limit', close_side, dust_qty,
                                    price=current_price,
                                    params={'timeInForce': 'GTC', 'newClientOrderId': close_cid}
                                )
                                if close_order:
                                    save_bot_order(
                                        bot_id=b.bot_id, order_type='close',
                                        exchange_order_id=close_order['id'],
                                        price=current_price, amount=dust_qty, step=0,
                                        status='open', client_order_id=close_cid,
                                        cycle_id=b.cycle_id
                                    )
                                    cursor.execute(
                                        "UPDATE trades SET cycle_phase='PARTIAL_CLOSE_PENDING' WHERE bot_id=?",
                                        (b.bot_id,)
                                    )
                                    logger.info(
                                        f"✅ [DUST-CHASER] Placed GTC close for {b.name}: "
                                        f"{dust_qty} {b.pair} @ {current_price}"
                                    )
                        except Exception as e_dust:
                            logger.error(f"[DUST-CHASER] Failed to place close for {b.name}: {e_dust}", exc_info=True)
                            # Escalate cycle_phase so the health check and UI banner surface this. [INV-35]
                            try:
                                cursor.execute(
                                    "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?",
                                    (b.bot_id,)
                                )
                                logger.critical(
                                    f"🚨 [STUCK_DUST_NO_EXIT] {b.name} (bot {b.bot_id}): dust-chaser GTC close "
                                    f"placement failed ({e_dust}). Manual exchange close required. "
                                    f"cycle_phase set to STUCK_DUST_NO_EXIT."
                                )
                            except Exception as _e_escalate:
                                logger.error(
                                    f"[DUST-CHASER] Failed to set STUCK_DUST_NO_EXIT "
                                    f"for bot {b.bot_id}: {_e_escalate}"
                                )
                        # Do NOT call safe_wipe_bot — the PARTIAL_CLOSE_PENDING handler in
                        # maintain_orders will reset the bot after the close fills
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                            action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Dust Chaser: Placed GTC close for ${b.total_invested:.2f} dust position."
                        ))
                        dust_cleared_any = True
                        continue



                if b.in_trade and b.total_invested > 1.0:

                    from .database import get_connection

                    try:

                        # Use cursor directly to speed up (minimal locking)

                        conn = get_connection()

                        cursor = conn.cursor()

                        cursor.execute("SELECT status FROM bots WHERE id=?", (b.bot_id,))

                        status_row = cursor.fetchone()

                        if status_row and status_row[0].upper() == 'SCANNING':

                            logger.info(f"🔧 [Consensus] Unblocking Bot {b.name} (ID {b.bot_id}) from 'Scanning' to 'IN TRADE'.")

                            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (b.bot_id,))

                            cursor.execute("UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (b.bot_id,))

                            conn.commit()

                    finally:

                        if 'conn' in locals(): pass # conn.close() disabled for singleton safety



            if dust_cleared_any:

                logger.info(f"🧹 [DUST-CHASER] State altered for {pair}. Deferring remaining parity checks to next tick.")

                continue # Skip remaining parity checks since state was altered



            # 🛡️ ARCHITECTURAL: VIRTUAL CONSENSUS GUARD

            # In One-Way Mode, if the Sum of System Ledgers matches the Exchange Net,

            # then all bots are mathematically healthy (Virtual Hedging).

            # Exit immediately to suppress individual bot side-mismatch alerts.

            if delta_qty < QTY_EPSILON:

                if len(bots) > 1:

                    logger.info(f"🛡️ [VIRTUAL-CONSENSUS] {pair}: {len(bots)} bots net to {virtual_net_qty:.6f}, matching Exchange perfectly. Stability confirmed.")

                continue  # All healthy, skip to next pair

            genuine_in_trade_bots = [b for b in bots if b.in_trade]



            # is_sole_direction must be computed BEFORE the if-block.

            # It checks if there is exactly ONE active bot for the direction of the error.

            phys_dir = 'LONG' if physical_net_qty > 0 else ('SHORT' if physical_net_qty < 0 else 'FLAT')

            virt_dir = 'LONG' if virtual_net_qty > 0 else ('SHORT' if virtual_net_qty < 0 else 'FLAT')

            

            # 🚀 ROOT CAUSE FIX: Ambiguous Error Side

            # If physical > virtual, we are MISSING virtual mass. The bot to adopt is on the physical side.

            # If virtual > physical, we have GHOST virtual mass. The bot to wipe is on the virtual side.

            if abs(virtual_net_qty) < abs(physical_net_qty):

                error_side = phys_dir

            else:

                error_side = virt_dir

                

            if error_side == 'FLAT': # Fallback if exactly matched magnitudes but wrong side (rare)

                error_side = 'LONG' if (virtual_net_qty - physical_net_qty) > 0 else 'SHORT'

                

            # Find the bot governing the error direction

            governing_bots = [b for b in bots if b.is_active and b.direction.upper() == error_side]

            is_sole_direction = len(governing_bots) == 1

            bot = governing_bots[0] if is_sole_direction else None



            # 🚀 ROOT CAUSE FIX: Do not require `genuine_in_trade_bots` to be non-empty. 

            # If the database was wiped, ALL bots will be `in_trade=False`, but the physical gap STILL EXISTS!

            # The system must process the gap and run deduction to recover the orphaned physical positions.

            if delta_qty > QTY_EPSILON and (abs(virtual_net_qty) > QTY_EPSILON or abs(physical_net_qty) > QTY_EPSILON):



                  logger.error(

                      f"🚨 [QTY-GAP] {pair}: Virtual={virtual_net_qty:.6f} vs "

                      f"Physical={physical_net_qty:.6f} (Delta={delta_qty:.6f} units, ${delta_notional:.2f}). "

                      f"Scanning for physically impossible states..."

                  )

                  

                  # 🚀 GLOBAL FLATTENING OVERRIDE (Applies to exactly 0.00 physical):

                  # If the exchange physically holds absolutely zero contracts, then NO active bot can hold mass.

                  # This catches anomalies when the user clicks 'Market Close' sequentially on multi-bot pairs.

                  if abs(physical_net_qty) < QTY_EPSILON:

                      # Guard B: Verify snapshot freshness (< 60 seconds old) before acting
                      bypass_guards = False
                      try:
                          db_conn = get_connection()
                          max_checked_row = db_conn.execute("SELECT MAX(last_checked) FROM active_positions").fetchone()
                          max_checked = max_checked_row[0] if max_checked_row else None
                      except Exception as db_err:
                          logger.warning(f"Error checking active_positions freshness: {db_err}")
                          max_checked = None
                          bypass_guards = True

                      if not bypass_guards:
                          current_ts = int(time.time())
                          if not max_checked or (current_ts - max_checked) > 60:
                              logger.warning(
                                  f"🛑 [GLOBAL-FLATTEN-DEFERRED] Snapshot too old to confirm flat position on {pair}. "
                                  f"last_checked={max_checked}, age={current_ts - (max_checked or 0)}s"
                              )
                              continue  # Skip global flatten action this cycle

                          # Guard A: Require 3 consecutive flat snapshots before acting
                          if not hasattr(self, '_flat_snapshots_counts'):
                              self._flat_snapshots_counts = {}
                          self._flat_snapshots_counts[pair] = self._flat_snapshots_counts.get(pair, 0) + 1
                          if self._flat_snapshots_counts[pair] < 3:
                              logger.warning(
                                  f"⏳ [GLOBAL-FLATTEN-DEFERRED] {pair} is flat but waiting for 3 consecutive flat snapshots. "
                                  f"Current consecutive count: {self._flat_snapshots_counts[pair]}/3."
                              )
                              continue  # Skip global flatten action this cycle



                      for b in bots:

                          if not b.in_trade: continue

                          # 🛡️ MANUAL GATE GUARD: Never auto-flatten a gated bot.

                          # REQUIRE_MANUAL_PROOF bots are already halted — the exchange may be

                          # zero because the reconciler or user flattened the physical position

                          # AFTER the gate was raised. Wiping the DB ledger here would cause

                          # open_qty to re-diverge on the next recompute cycle. Human must

                          # verify and clear the gate explicitly.

                          if b.bot_status.upper() == 'REQUIRE_MANUAL_PROOF':

                              logger.warning(
                                  f"🛑 [GLOBAL-FLATTEN SKIPPED] {b.name}: Bot is in REQUIRE_MANUAL_PROOF state. "
                                  f"Skipping auto-wipe — human intervention required before ledger can be reset."
                              )

                              continue

                          logger.warning(f"🛡️ [GLOBAL-FLATTEN] Exchange physically holds 0.0 units for {pair}. Auto-zeroing orphaned Bot {b.name}.")

                          

                          # 1. Search for proof of exit

                          proof = self._find_proof_of_exit(b)

                          if proof:

                              logger.info(f"✅ [GLOBAL-FLATTEN PROOF FOUND] Found manual exit or liquidation order: {proof.get('id')}")

                          else:

                              # 2. No proof found — but physical IS already 0.

                              logger.warning(

                                  f"⚡ [GLOBAL-FLATTEN AUTO-WIPE] {b.name}: Physical is 0.0. "

                                  f"No proof of exit found, but exchange is ground truth. "

                                  f"Auto-clearing virtual ledger (${b.total_invested:.2f})."

                              )

                              proof = None  # Allow wipe path to proceed



                          from .database import log_reconciliation

                          # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() gate

                          wiped = safe_wipe_bot(

                              b.bot_id, b.pair, b.direction,

                              reason="GLOBAL_FLATTEN: Proof of exit verified",

                              exit_price=0.0,

                              force=True,

                              human_approved=True,

                              cursor=cursor

                          )

                          if wiped:

                              log_reconciliation(

                                  bot_id=b.bot_id, pair=pair, action="RESET_WITH_PROOF",

                                  details=f"Global Flatten: Verified exit via proof order. Ledger zeroed."

                              )

                              results.append(ReconciliationResult(

                                  bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                  details=f"Global Flatten: Reset bot using verified proof of exit.", requires_manual_intervention=False

                               ))

                      continue # Skip following check as bot state handled
                  else:
                      if not hasattr(self, '_flat_snapshots_counts'):
                          self._flat_snapshots_counts = {}
                      self._flat_snapshots_counts[pair] = 0

                  

                  # 🚀 ONE-WAY SIDE-PRUNE (SOLE-BOT ONLY):

                  # In One-Way mode, a SHORT bot cannot logically hold units if the exchange is Net LONG.

                  # CRITICAL: This logic is ONLY valid for sole-bot pairs (one bot per pair).

                  # For multi-bot pairs, both LONG and SHORT bots naturally coexist — the exchange

                  # net is LONG - SHORT, which may be positive even when SHORT bots are active.

                  # Applying SIDE-PRUNE to multi-bot pairs incorrectly zeroes out legitimate SHORT bots.

                  phys_side = 'LONG' if physical_net_usd > 0 else ('SHORT' if physical_net_usd < 0 else 'FLAT')

                  

                  if is_sole:  # ← GATE: Only sole-bot pairs can be SIDE-PRUNED

                      for b in bots:

                          if not b.in_trade: continue

                          bot_side = b.direction.upper()

                          if phys_side != 'FLAT' and bot_side != phys_side:

                              # Sole-bot on the WRONG side of physical reality.

                              # 🚀 ANTI-NUKE FIX: We no longer auto-wipe. Flag for manual review.

                              logger.warning(f"🛡️ [SIDE-MISMATCH] Bot {b.name} ({bot_side}) is physically opposite to Exchange ({phys_side}). Possible manual trade or ghost.")

                              

                              from .database import log_reconciliation

                              log_reconciliation(

                                  bot_id=b.bot_id, pair=pair, action="SIDE_MISMATCH",

                                  details=f"Bot is {bot_side}, Exchange is {phys_side}. AUTO-WIPE DISABLED for safety."

                               )

                              results.append(ReconciliationResult(

                                  bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.REQUIRE_MANUAL,

                                  details=f"Side Mismatch: Bot is {bot_side}, Exchange is {phys_side}. Check for manual trades.",

                                  requires_manual_intervention=True

                              ))

                  else:

                      # Multi-bot pair: Physical > Virtual gap = offline grid fills NOT in trades table.

                      # Physical < Virtual gap would be a ghost — but that is handled by Case B.2 (structural ghost).

                      # 🚀 FIX: Compare absolute magnitudes to correctly handle SHORT positions (negative values).

                      phys_mag = abs(physical_net_usd)

                      virt_mag = abs(virtual_net_usd)

                      phys_gap = phys_mag - virt_mag  # positive = physical has more mass

                      

                      if phys_gap > 5.0:

                          # 🚀 FUNDAMENTAL FIX: Physical > Virtual on multi-bot pair means missed fills.

                          # Trigger a targeted offline fill reconstruction for this specific pair.

                          # reconstruct_offline_fills already has CQB_-receipt proof — it will only

                          # adopt fills that belong to this system's own signed orders.

                          logger.warning(

                              f"⚙️ [MULTIBOT-FILLSCAN] {pair}: Physical Mag=${phys_mag:.2f} > Virtual Mag=${virt_mag:.2f} "

                              f"by ${phys_gap:.2f}. Missed fills detected. Triggering targeted 30-day offline fill reconstruction."

                          )

                          try:

                              self.reconstruct_offline_fills(since_hours=720, pair_filter=pair)

                          except Exception as _rfill_err:

                              logger.error(f"❌ [MULTIBOT-FILLSCAN] reconstruct_offline_fills failed for {pair}: {_rfill_err}")

                      elif phys_gap < -5.0:

                          # 🚀 ROOT CAUSE FIX: Strict ID-Based Tracking Doctrine.

                          # We do NOT pass this to guesswork. If physical mass is missing, it is a catastrophic external drift.

                          logger.warning(f"⚠️ [MISSING-MASS] {pair}: Virtual Ledger (${virt_mag:.2f}) > Exchange Physical (${phys_mag:.2f}). An unauthorized external trade occurred! Manual intervention required.")



            

            # ------------------------------------------------------------------

            # CASE D: NET-SUM GHOST DETECTION (Revised Fix #1)

            # ------------------------------------------------------------------

            # Instead of strict direction checks (which break hedging), we check

            # the Net Error. If Virtual is too LONG vs Physical, we check LONG bots.

            # If a suspect bot has NO open orders, it's a ghost.

            # ------------------------------------------------------------------

            

            # 1. Calculate Net Mismatch

            # Virtual Net = (Longs) - (Shorts) in USD

            # Physical Net = (Longs) - (Shorts) in USD

            virt_net = virtual_net_usd

            phys_net = physical_net_usd

            net_error = virt_net - phys_net

            

            # 🚀 FIX: is_sole was computed once earlier in Case C (before the if block).

            # threshold is now QTY-based: delta_qty > QTY_EPSILON (already computed above).

            # This replaces the old $50 USD threshold which silently ignored sub-$50 gaps.

                

            # 4. Handle Mismatch or Consensus

            if delta_qty > QTY_EPSILON:

                logger.warning(f"⚠️ [NET-MISMATCH] {pair_normalized}: Virtual={virtual_net_qty:.6f} units, Physical={physical_net_qty:.6f} units. Gap={delta_qty:.6f} (${delta_notional:.2f}).")



                # 🧹 Pre-emptive Dust Chaser removed. Unified under Scenario A/B block below.



                # Consensus Strategy A: Directional Confident Adoption (REMOVED)

                if is_sole_direction and bot:

                    if bot.direction.upper() == phys_dir or phys_dir == 'FLAT':

                        # 🚀 PROFESSOR'S CONFIDENT ADOPTION (Directional) has been removed.

                        # Pure guessing is strictly forbidden under v2.5.0 Proof-Only Consensus.

                        # We no longer blindly adopt unproven ledger gaps. We must fall through

                        # to Strategy B.4 which requires cryptographically verified DNA (Order IDs).

                        logger.warning(f"🚫 [ADOPTION-BLOCKED] {pair_normalized}: Directional guessing removed. Falling through to Proof-Only B.4.")

                    else:

                        logger.warning(f"🚫 [ADOPTION-BLOCKED] {pair_normalized}: Physical is {phys_dir} but governing bot {bot.name} is {bot.direction.upper()}.")

                        # Falls through to Strategy B/C for manual intervention flag



                # ================================================================

                # 🔑 Consensus Strategy B.4: Dual-Proof DNA Attribution (RUNS FIRST)

                # ================================================================

                # B.4 has FULL AUTHORITY for multi-bot pairs.

                # It runs BEFORE B.5 so TP order proof takes precedence over guesses.

                #

                # PROOF 1 — TP open order amount:

                #   When a bot places a TP order it uses EXACTLY the qty it holds.

                #   client_order_id = CQB_{bot_id}_TP_... so we know owner + size.

                # PROOF 2 — bot_orders cycle ledger:

                #   SUM(filled entries) - SUM(filled exits) for this bot's current cycle.

                # Cross-referencing both gives maximum confidence.

                #

                # B.4 ALSO CLEARS PHANTOMS: any bot claiming a position with ZERO

                # evidence from both proofs is reset to 0 (it's a ghost).

                # ================================================================

                # 🚀 ROOT CAUSE FIX [v2.5.2]: Correctly identify error side for Orphans vs Ghosts

                # If physical magnitude > virtual magnitude, we have an orphaned physical position.

                # The error is on the physical side (phys_dir).

                # If virtual magnitude > physical magnitude, we have a ghost virtual position.

                # The error is on the virtual side (virt_dir).

                if abs(physical_net_qty) > abs(virtual_net_qty) + QTY_EPSILON:

                    error_side = phys_dir

                else:

                    error_side = virt_dir

                    

                if error_side == 'FLAT':

                    error_side = 'LONG' if net_error > 0 else 'SHORT'



                # Pre-calculate physical capacity for the error direction

                opposite_tracked_qty_for_b4 = sum(b.total_invested / b.avg_entry_price for b in bots if b.in_trade and b.avg_entry_price > 0 and b.direction.upper() != error_side)

                if error_side == 'LONG':

                    b4_max_capacity_qty = physical_net_qty + opposite_tracked_qty_for_b4

                else:

                    b4_max_capacity_qty = -(physical_net_qty - opposite_tracked_qty_for_b4)



                pair_open_orders = all_orders.get(pair_normalized, [])



                # Collect TP amount proof from live open orders (Proof 1)

                # ExchangeOrder objects use .client_order_id and .amount attributes

                tp_proof: dict = {}  # bot_id -> {'tp_qty': float, 'labels': list}

                for o in pair_open_orders:

                    cid = o.client_order_id or ''

                    if not cid.startswith('CQB_'):

                        continue

                    parts = cid.split('_')

                    if len(parts) < 3:

                        continue

                    try:

                        claim_bot_id = int(parts[1])

                    except ValueError:

                        continue

                    order_type_tag = parts[2] if len(parts) > 2 else ''

                    # ExchangeOrder.amount = origQty (equals exact position qty for TP orders)

                    order_qty = float(o.amount or 0)

                    if claim_bot_id not in tp_proof:

                        tp_proof[claim_bot_id] = {'tp_qty': 0.0, 'labels': []}

                    tp_proof[claim_bot_id]['labels'].append(cid)

                    if order_type_tag in ('TP', 'SL', 'STOP', 'CLOSE', 'FLATTEN'):

                        tp_proof[claim_bot_id]['tp_qty'] += order_qty



                # Collect cycle ledger proof from bot_orders (Proof 2)

                ledger_proof: dict = {}  # bot_id -> net_qty

                try:

                    from .database import get_connection

                    _lconn = get_connection()

                    _lcur = _lconn.cursor()

                    bots_on_pair = [str(b.bot_id) for b in bots if normalize_symbol(b.pair) == pair_normalized]

                    if bots_on_pair:

                        placeholders = ','.join('?' * len(bots_on_pair))

                        _lcur.execute(f"""

                            SELECT bo.bot_id,

                                   SUM(CASE WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount ELSE 0 END) as entry_qty,

                                   SUM(CASE WHEN bo.order_type IN ('tp', 'exit', 'close', 'adoption_reduce', 'dust_close', 'sl', 'flatten_close') THEN bo.filled_amount ELSE 0 END) as exit_qty

                            FROM bot_orders bo
                            JOIN trades t ON t.bot_id = bo.bot_id

                            WHERE bo.bot_id IN ({placeholders})
                              AND bo.cycle_id = t.cycle_id
                              AND (bo.status IN ('filled','closed','auto_closed','hedge_exited','partially_filled') OR (bo.status IN ('cancelled','canceled') AND bo.filled_amount > 0))
                              AND bo.filled_amount > 0

                            GROUP BY bo.bot_id

                        """, tuple(bots_on_pair))

                    for row in _lcur.fetchall():

                        _bid, _entry_qty, _exit_qty = row

                        net = (_entry_qty or 0) - (_exit_qty or 0)

                        if net > 0.00001:

                            ledger_proof[_bid] = net

                    pass # _lconn.close() disabled for singleton safety

                except Exception as _le:

                    logger.warning(f"⚠️ [DNA-B4] Could not read bot_orders ledger: {_le}")



                all_claimant_ids = set(list(tp_proof.keys()) + list(ledger_proof.keys()))

                b4_ran = False



                if all_claimant_ids:

                    logger.info(f"🔑 [DNA-B4] Claimants for {pair_normalized}: TP-proof={list(tp_proof.keys())} Ledger-proof={list(ledger_proof.keys())}")

                    adopted_via_dna = False

                    sign = -1 if error_side == 'SHORT' else 1

                    ref_pos = next((p for p in pair_positions), None)

                    current_price = abs(ref_pos.entry_price) if ref_pos and abs(ref_pos.entry_price or 0) > 0 else (abs(physical_net_usd / physical_net) if physical_net else 0)



                    for claim_bot_id in all_claimant_ids:

                        matching = [b for b in bots if b.bot_id == claim_bot_id]

                        if not matching:

                            continue

                        claim_bot = matching[0]



                        if claim_bot.in_trade and claim_bot.total_invested > 1.0:

                            # 🚀 STALE-STEP CHECK: Even if the bot IS tracked, its step count may be behind.

                            # Example: DB thinks step 1 ($141), exchange has step 3 ($425).

                            # If physical for this direction is >10% more than the bot's virtual holding,

                            # this bot has UNrecorded grid fills. Trigger fill reconstruction.

                            bot_virtual_qty = (claim_bot.total_invested / claim_bot.avg_entry_price

                                               if claim_bot.avg_entry_price > 0 else 0.0)

                            phys_matched_qty = sum(

                                abs(p.size) for p in pair_positions

                                if p.side == claim_bot.direction.upper()

                            )

                            if phys_matched_qty > bot_virtual_qty * 1.10:

                                logger.warning(

                                    f"⚙️ [DNA-B4-STALE] Bot {claim_bot.name} is tracked at {bot_virtual_qty:.6f} units "

                                    f"but exchange shows {phys_matched_qty:.6f} for {claim_bot.direction}. "

                                    f"Step count is stale — triggering offline fill reconstruction."

                                )

                                try:

                                    self.reconstruct_offline_fills(

                                        since_hours=168,

                                        pair_filter=normalize_symbol(claim_bot.pair)

                                    )

                                except Exception as _stale_err:

                                    logger.error(f"❌ [DNA-B4-STALE] Fill scan failed for {claim_bot.name}: {_stale_err}")

                            else:

                                logger.info(f"✅ [DNA-B4] Bot {claim_bot.name} already tracked (${claim_bot.total_invested:.2f}). Skipping.")

                            continue



                        tp_qty = tp_proof.get(claim_bot_id, {}).get('tp_qty', 0.0)

                        ledger_qty = ledger_proof.get(claim_bot_id, 0.0)

                        

                        # 🚀 SAFETY GATE: Reject mathematically impossible proof

                        # Note: We only reject if magnitude shrinks (Partial Vanish). 

                        if claim_bot.direction.upper() == error_side and abs(phys_net) < abs(virt_net):

                            if tp_qty > b4_max_capacity_qty + 0.0001:

                                logger.warning(f"⚠️ [DNA-B4 OVERCLAIM] Bot {claim_bot.name} claims {tp_qty:.6f} via TP proof, but remaining capacity for {error_side} is only {b4_max_capacity_qty:.6f}. Rejecting.")

                                tp_qty = 0.0

                            if ledger_qty > b4_max_capacity_qty + 0.0001:

                                ledger_qty = 0.0



                        if tp_qty > 0 and ledger_qty > 0:

                            if abs(tp_qty - ledger_qty) > 0.0001:

                                logger.warning(f"⚠️ [DNA-B4] Bot {claim_bot.name}: TP proof ({tp_qty:.6f}) differs from ledger ({ledger_qty:.6f}). Using TP (live).")

                            proven_qty = tp_qty

                            proof_source = f"TP+Ledger ({tp_qty:.6f} vs {ledger_qty:.6f})"

                        elif tp_qty > 0:

                            proven_qty = tp_qty

                            proof_source = f"TP order ({tp_qty:.6f} units)"

                        elif ledger_qty > 0:

                            proven_qty = ledger_qty

                            proof_source = f"Ledger ({ledger_qty:.6f} units)"

                        else:

                            logger.warning(f"⚠️ [DNA-B4] Bot {claim_bot.name}: Signed orders found but qty=0 in both proofs. Cannot adopt.")

                            continue



                        proven_usd = proven_qty * current_price

                        labels = tp_proof.get(claim_bot_id, {}).get('labels', [])

                        logger.info(f"✅ [DNA-B4] PROOF: Bot {claim_bot.name} (ID {claim_bot_id}) holds {proven_qty:.6f} units (${proven_usd:.2f}). Source: {proof_source}. Orders: {labels}")

                        self._execute_accounting_adjustment(claim_bot, sign * proven_usd, sign * proven_qty, f"DNA B.4: {proof_source}")

                        

                        # 🚀 SUBTRACT ADOPTED CAPACITY: Prevent multiple clones from claiming the same 

                        # physical mass by draining the bucket as each valid claim is processed.

                        if claim_bot.direction.upper() == error_side:

                            b4_max_capacity_qty -= proven_qty

                            

                        results.append(ReconciliationResult(

                            bot_id=claim_bot.bot_id, bot_name=claim_bot.name, pair=pair_normalized,

                            action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                            details=f"DNA B.4 Attribution: {proven_qty:.6f} qty adopted to {claim_bot.name}. Source: {proof_source}.",

                            requires_manual_intervention=False

                        ))

                        adopted_via_dna = True
                    b4_ran = True




                # ──────────────────────────────────────────────────────────────

                # PHANTOM CLEAR: Any bot claiming a position with ZERO evidence

                # from BOTH proofs is a ghost — reset it to 0.

                # This handles the "SUI long phantom" and similar stale DB states.

                # ──────────────────────────────────────────────────────────────

                for bot in bots:

                    if not (bot.in_trade and bot.total_invested > 1.0):

                        continue  # Not claiming any position — nothing to clear

                    bid = bot.bot_id

                    has_tp_proof = tp_proof.get(bid, {}).get('tp_qty', 0.0) > 0

                    has_ledger_proof = ledger_proof.get(bid, 0.0) > 0

                    has_any_signed_order = bid in tp_proof  # At least exists in signed order map

                    if not has_tp_proof and not has_ledger_proof and not has_any_signed_order:

                        logger.warning(f"⚠️ [DNA-B4 ORPHAN-STATE] Bot {bot.name} claims ${bot.total_invested:.2f} but has ZERO TP or ledger evidence. Missing mass cannot be auto-wiped under strict tracking.")

                        # 🚀 ROOT CAUSE FIX: Do not guess it's a phantom to auto-wipe. Demand manual proof.

                        results.append(ReconciliationResult(

                            bot_id=bot.bot_id, bot_name=bot.name, pair=pair_normalized,

                            action_taken=ReconciliationAction.REQUIRE_MANUAL,

                            details=f"Missing Evidence: Bot {bot.name} claims position but lacks cryptographic ledger tracking. Manual intervention required.",

                            requires_manual_intervention=True

                        ))

                        b4_phantom_cleared = True



                if b4_ran:

                    # ──────────────────────────────────────────────────────────

                    # 🚀 RESIDUAL SIDE-ORPHAN DETECTOR [v2.5.1]

                    # ──────────────────────────────────────────────────────────

                    # B.4 resolved the "dominant" net-gap claimant, but in a

                    # multi-bot pair the NET calculation can mask an orphan on

                    # the OPPOSITE side.

                    #

                    # Example: BTC/USDC has bots 10016 (LONG 1.222) + 10022 (SHORT, scanning).

                    #   Physical: LONG 1.222, SHORT 0.002

                    #   Virtual:  LONG 1.222, SHORT 0.000

                    #   Net:      physical=1.220, virtual=1.222 → gap=0.002 LONG-error

                    #   B.4 finds 10016 TP-proof → adopts/confirms 1.222, marks b4_ran=True.

                    #   But exchange still has 0.002 SHORT with NO bot owner → the orphan.

                    #

                    # Fix: For each position side present on the exchange, compute the

                    # total virtual tracking for that side. If exchange_side_qty > virtual_side_qty

                    # and no active bot in that side is scanning (i.e. all in-trade bots for that

                    # side have positive invested), the gap is a true orphaned position — market close it.

                    # ──────────────────────────────────────────────────────────

                    try:

                        _ex_heal = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)

                        for _orphan_side in ('LONG', 'SHORT'):

                            # Sum physical qty on this side

                            _phys_side_qty = sum(

                                abs(p.size) for p in pair_positions if p.side == _orphan_side

                            )

                            if _phys_side_qty < QTY_EPSILON:

                                continue  # No physical position on this side → nothing to check



                            # Sum virtual tracking for all bots on this side

                            _virt_side_qty = sum(

                                (b.total_invested / b.avg_entry_price)

                                for b in bots

                                if b.direction.upper() == _orphan_side

                                and b.in_trade

                                and b.avg_entry_price > 0

                            )



                            _side_residual = _phys_side_qty - _virt_side_qty

                            # Calculate pending reduce/close qty on this side
                            _reduce_side = 'sell' if _orphan_side == 'LONG' else 'buy'
                            _bots_this_side_ids = [str(b.bot_id) for b in bots if b.direction.upper() == _orphan_side]
                            _pending_reduce_qty = 0.0
                            if _bots_this_side_ids:
                                for _ord in pair_open_orders:
                                    _o_side = str(_ord.side or '').lower()
                                    if _o_side != _reduce_side:
                                        continue
                                    _o_cid = str(_ord.client_order_id or '')
                                    if not _o_cid.startswith('CQB_'):
                                        continue
                                    _o_parts = _o_cid.split('_')
                                    if len(_o_parts) < 3:
                                        continue
                                    _o_bid = _o_parts[1]
                                    _o_tag = _o_parts[2]
                                    if _o_bid in _bots_this_side_ids and _o_tag in ('TP', 'SL', 'STOP', 'CLOSE', 'FLATTEN'):
                                        _pending_reduce_qty += float(_ord.amount or 0)

                            _side_residual -= _pending_reduce_qty

                            if _side_residual < QTY_EPSILON:

                                continue  # Physical fully explained by virtual tracking and pending reduce orders



                            # Residual exists! Check if any scanning bot on this side holds it

                            _scanning_bots_this_side = [

                                b for b in bots

                                if b.direction.upper() == _orphan_side and not b.in_trade

                            ]



                            logger.warning(

                                f"🔍 [RESIDUAL-ORPHAN] {pair_normalized} {_orphan_side}: "

                                f"Physical={_phys_side_qty:.6f}, Virtual={_virt_side_qty:.6f}, "

                                f"Residual={_side_residual:.6f}. Scanning bots on this side: "

                                f"{[b.name for b in _scanning_bots_this_side]}. Evaluating close."

                            )



                            # Only auto-close if the residual notional is below a safe threshold

                            # (prevents accidental nuke of large legitimate positions)

                            _orphan_price = next(

                                (p.entry_price for p in pair_positions if p.side == _orphan_side), 0.0

                            ) or (abs(physical_net_usd / physical_net_qty) if physical_net_qty else 0.0)

                            _orphan_notional = _side_residual * _orphan_price



                            _min_notional, _min_qty = self._get_dust_thresholds(pair_normalized)



                            if _orphan_notional > 500.0:

                                # Too large to auto-close without manual confirmation

                                logger.warning(

                                    f"⚠️ [RESIDUAL-ORPHAN] {pair_normalized}: Orphan {_orphan_side} "

                                    f"${_orphan_notional:.2f} too large (>$500) for auto-close. "

                                    f"Manual intervention required."

                                )

                                results.append(ReconciliationResult(

                                    bot_id=0, bot_name=f"ORPHAN-{_orphan_side}",

                                    pair=pair_normalized,

                                    action_taken=ReconciliationAction.REQUIRE_MANUAL,

                                    details=(

                                        f"Residual Orphan: {_orphan_side} {_side_residual:.6f} "

                                        f"(${_orphan_notional:.2f}) has no DB owner and exceeds auto-close threshold. "

                                        f"Manually close this position or run Forensic Adopt."

                                    ),

                                    requires_manual_intervention=True

                                ))

                                continue



                            # Safe to auto-close
                            # User requested no automatic emergency close on startup.
                            # We bypass auto-close and escalate in production, but allow it in testing mode.
                            from config.settings import config as app_cfg_reconciler
                            if not getattr(app_cfg_reconciler, 'TESTING_MODE', False):
                                logger.warning(
                                    f"[RESIDUAL-ORPHAN-BYPASS] Auto-close bypassed by user request for {pair_normalized}: "
                                    f"{_orphan_side} {_side_residual:.6f}. Escalate to REQUIRE_MANUAL."
                                )
                                results.append(ReconciliationResult(
                                    bot_id=0, bot_name=f"ORPHAN-{_orphan_side}",
                                    pair=pair_normalized,
                                    action_taken=ReconciliationAction.REQUIRE_MANUAL,
                                    details=(
                                        f"Residual Orphan: {_orphan_side} {_side_residual:.6f} "
                                        f"(${_orphan_notional:.2f}) bypassed auto-close by request."
                                    ),
                                    requires_manual_intervention=True
                                ))
                                continue

                            _close_side = 'buy' if _orphan_side == 'SHORT' else 'sell'

                            _closed = False

                            if _ex_heal:

                                _ro_audit_conn = get_connection()

                                _ro_audit_cur = _ro_audit_conn.cursor()

                                try:

                                    _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                                    _ex_heal.create_order(

                                        symbol=_ccxt_pair,

                                        type='market',

                                        side=_close_side,

                                        amount=_side_residual,

                                        params=_safe_order_params({'reduceOnly': True}, _is_testnet),

                                        emergency=True,

                                        _audit_cursor=_ro_audit_cur,

                                        _call_site="resolve_net_mismatch._residual_orphan_close:2335",

                                    )

                                    _ro_audit_conn.commit()

                                    logger.info(

                                        f"✅ [RESIDUAL-ORPHAN-CLOSE] {pair_normalized}: "

                                        f"Market {_close_side.upper()} {_side_residual:.6f} "

                                        f"executed to close orphaned {_orphan_side} position."

                                    )

                                    _closed = True

                                except Exception as _roe:

                                    if '-4061' in str(_roe) or 'positionside' in str(_roe).lower():

                                        # Retry without positionSide if rejected

                                        try:

                                            _ex_heal.create_order(

                                                symbol=_ccxt_pair, type='market',

                                                side=_close_side, amount=_side_residual,

                                                params={'reduceOnly': True},

                                                emergency=True,

                                                _audit_cursor=_ro_audit_cur,

                                                _call_site="resolve_net_mismatch._residual_orphan_close_retry:2352",

                                            )

                                            _ro_audit_conn.commit()

                                            _closed = True

                                        except Exception as _e:
                                            logger.debug(f'[EXPECTED] [RESIDUAL-ORPHAN-CLOSE-RETRY] positionSide-stripped retry failed: {_e}')

                                    if not _closed:

                                        logger.error(f"❌ [RESIDUAL-ORPHAN-CLOSE] {pair_normalized}: Failed: {_roe}")



                            results.append(ReconciliationResult(

                                bot_id=0, bot_name=f"ORPHAN-{_orphan_side}",

                                pair=pair_normalized,

                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                details=(

                                    f"Residual Orphan Closed: {_orphan_side} {_side_residual:.6f} "

                                    f"(${_orphan_notional:.2f}). Exchange close={'SUCCESS' if _closed else 'FAILED'}."

                                ),

                                requires_manual_intervention=not _closed

                            ))

                    except Exception as _rod_err:

                        logger.warning(f"[RESIDUAL-ORPHAN-DETECTOR] Non-fatal error for {pair_normalized}: {_rod_err}")

                    # ── End Residual Side-Orphan Detector ─────────────────────

                    continue  # B.4 fully resolved claimants — skip B.5 and Strategy C





                # Calculate adjusted physical quantities BEFORE B.5 / Strategy C

                opposite_tracked_usd = sum(b.total_invested for b in bots if b.in_trade and b.direction.upper() != error_side)

                opposite_tracked_qty = sum(b.total_invested / b.avg_entry_price for b in bots if b.in_trade and b.avg_entry_price > 0 and b.direction.upper() != error_side)

                

                if error_side == 'LONG':

                    adjusted_phys_usd = physical_net_usd + opposite_tracked_usd

                    adjusted_phys_qty = physical_net_qty + opposite_tracked_qty

                else:

                    adjusted_phys_usd = physical_net_usd - opposite_tracked_usd

                    adjusted_phys_qty = physical_net_qty - opposite_tracked_qty



                # ================================================================

                # 🚀 FUNDAMENTAL FIX: "Proof-Only" Market Flatten Protocol

                # If B.4 (Cryptographic Order-ID Proof) found no owners for the gap,

                # the system MUST NOT guess via deduction. The DB/Exchange state is corrupted.

                # Action: 

                # 1. Trigger Force-SL on all bots in the error direction (they sell their virtual mass).

                # 2. If NO bots share the direction, the gap is purely orphaned physical mass,

                #    so we execute a direct physical Market Close to heal the exchange to 0.

                # ================================================================

                if not b4_ran:

                    # ================================================================

                    # 🚀 FUNDAMENTAL FIX: "Strict Forensic Parity" Protocol [V1.6.7]

                    # ================================================================

                    # 1. FINAL FORENSIC ATTEMPT: Trigger deep paged scan to find missing fills.

                    logger.info(f"🔎 [FORENSIC-SCAN] {pair_normalized}: Missing proof. Triggering 7-day deep fill reconstruction...")

                    try:

                        self.reconstruct_offline_fills(since_hours=168, pair_filter=pair_normalized)

                        # Re-run DNA sync to refresh local ledger view

                        self._align_memory_to_ledger()

                    except Exception as _fsc_err:

                        logger.error(f"❌ [FORENSIC-SCAN] Failed: {_fsc_err}")



                    # 2. EVALUATE RECOVERED STATE: Check if reconstruction solved the gap.

                    UNFLATTABLE_STATUSES = {
                        'REQUIRE_MANUAL_PROOF', 'MANUAL_GATE', 'FLATTENING',
                        'HEDGE_STANDBY', 'STOPPED'
                    }
                    suspects = [
                        b for b in bots
                        if b.in_trade
                        and b.direction.upper() == error_side
                        and str(b.bot_status).upper() not in UNFLATTABLE_STATUSES
                        and b.is_active
                    ]
                    if not suspects:
                        logger.warning(
                            f"[GLOBAL-FLATTEN BLOCKED] {pair_normalized}: All candidate bots are "
                            f"gated or inactive. Setting REQUIRE_MANUAL_PROOF instead of flattening."
                        )
                        for b in bots:
                            if b.in_trade and b.direction.upper() == error_side:
                                flag_bot_manual_proof(b.bot_id, reason='Global flatten blocked — bot is gated')
                        results.append(ReconciliationResult(
                            bot_id=0, bot_name=pair_normalized, pair=pair_normalized,
                            action_taken=ReconciliationAction.REQUIRE_MANUAL,
                            details=f"[GLOBAL-FLATTEN BLOCKED] All candidate bots on {pair_normalized} are gated or inactive.",
                            requires_manual_intervention=True
                        ))
                        continue


                    

                    # 🚀 CORE FIX: Only draw absolute capacity if the physical mass is actually in the error_side direction!

                    if error_side == 'LONG':

                        phys_qty_abs = abs(max(0.0, adjusted_phys_qty))

                    else:

                        phys_qty_abs = abs(min(0.0, adjusted_phys_qty))

                        

                    bot_claimed_abs = sum((b.total_invested / b.avg_entry_price) for b in suspects if b.avg_entry_price > 0)

                    gap_qty_signed = phys_qty_abs - bot_claimed_abs



                    # 3. HIERARCHICAL HEALING: Dust vs Orphans

                    if abs(gap_qty_signed) < QTY_EPSILON:

                        logger.info(f"✅ [FORENSIC-PARITY] Gap resolved via scan for {pair_normalized}.")

                        continue



                    # Fetch dynamic dust limits

                    min_notional, min_qty = self._get_dust_thresholds(pair_normalized)

                    

                    # Fetch current price for notional threshold check

                    ex_heal = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)

                    _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                    current_price = ex_heal.get_last_price(_ccxt_pair) if ex_heal else 0.0

                    if current_price <= 0:

                        current_price = suspects[0].avg_entry_price if suspects else 0.0



                    gap_notional = abs(gap_qty_signed * current_price)

                    

                    if gap_notional < min_notional * 1.05 or abs(gap_qty_signed) < min_qty:

                        # 🧹 DUST CHASER PATH: Sole-bot residue detected.

                        logger.warning(f"🧹 [DUST-CHASER] {pair_normalized}: Orphan residue ${gap_notional:.2f} (<min) on sole-bot. Auto-flattening parity.")

                        

                        if ex_heal:

                            heal_side = 'sell' if error_side == 'LONG' else 'buy'

                            heal_pos_side = error_side  # 'LONG' or 'SHORT'

                            try:

                                # reduceOnly bypasses min_notional for sole bots (Closing Only)

                                _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                                _sb_audit_conn = get_connection()

                                _sb_audit_cur = _sb_audit_conn.cursor()

                                ex_heal.create_order(

                                    symbol=_ccxt_pair, type='market', side=heal_side, amount=abs(gap_qty_signed),

                                    params=_safe_order_params({'reduceOnly': True}, _is_testnet),

                                    emergency=True,

                                    _audit_cursor=_sb_audit_cur,

                                    _call_site="resolve_net_mismatch._dust_chaser_sole_bot:2446",

                                )

                                _sb_audit_conn.commit()

                                logger.info(f"✅ [DUST-CHASER] Physical {heal_side.upper()} executed. Zeroing ledger.")

                                

                                # Zero out any dusty bots for this pair

                                for d_bot in suspects:

                                    # safe_wipe_bot handles the ledger clearing correctly

                                    safe_wipe_bot(d_bot.bot_id, d_bot.pair, d_bot.direction, reason=f"DUST_CHASER: Residue ${gap_notional:.2f} < ${min_notional:.2f}", force=True, human_approved=True, cursor=cursor)

                                continue

                            except Exception as _heale:

                                logger.error(f"❌ [DUST-CHASER] Flatten failed: {_heale}")



                    # 🚀 Fix 2: Directional Guard [v3.0.7]

                    # If physical position direction contradicts the suspect bots' direction,

                    # DO NOT autonomously flatten. The state is too ambiguous.

                    # e.g. error_side is LONG but physical position is SHORT.

                    if (error_side == 'LONG' and adjusted_phys_qty < -0.0001) or \
                       (error_side == 'SHORT' and adjusted_phys_qty > 0.0001):

                        logger.critical(

                            f"🚨 [DIRECTIONAL-MISMATCH] {pair_normalized}: Physical position ({adjusted_phys_qty:.6f}) "

                            f"contradicts suspect bots' direction ({error_side}). Blocking autonomous flatten."

                        )

                        for b in suspects:

                            cursor.execute("UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=?", (b.bot_id,))

                        conn.commit()

                        

                        results.append(ReconciliationResult(

                            bot_id=0, bot_name=pair_normalized, pair=pair_normalized,

                            action_taken=ReconciliationAction.REQUIRE_MANUAL,

                            details=f"Directional Mismatch: Physical {adjusted_phys_qty:.6f} vs Bot {error_side}. Manual proof required.",

                            requires_manual_intervention=True

                        ))

                        continue



                    # 4. 🚀 AGGRESSIVE MARKET FLATTEN PROTOCOL [V2.0]

                    # If forensic proof failed, we no longer block healing.

                    # We execute a direct Market Close to restore FACT-ONLY PARITY.

                    logger.warning(f"🗡️ [MARKET-FLATTEN] {pair_normalized}: Forensic proof failed. Flattening to zero parity.")

                    

                    flatten_executed = False

                    if ex_heal:

                        # === DIRECTION REALITY CHECK ===

                        # Verify physical still exists in the error direction before placing order.

                        phys_is_long  = physical_net_qty > QTY_EPSILON

                        phys_is_short = physical_net_qty < -QTY_EPSILON



                        if error_side == 'LONG' and not phys_is_long:

                            logger.warning(f"[FLATTEN-SKIP] {pair_normalized}: error_side=LONG but physical is {'SHORT' if phys_is_short else 'FLAT'}. Self-healed.")

                            flatten_executed = True

                        elif error_side == 'SHORT' and not phys_is_short:

                            logger.warning(f"[FLATTEN-SKIP] {pair_normalized}: error_side=SHORT but physical is {'LONG' if phys_is_long else 'FLAT'}. Self-healed.")

                            flatten_executed = True

                        elif phys_qty_abs < QTY_EPSILON:

                            logger.info(f"✅ [MARKET-FLATTEN] {pair_normalized}: Physical footprint is 0.0. Skipping.")

                            flatten_executed = True

                        else:

                            flatten_side = 'sell' if error_side == 'LONG' else 'buy'

                            # Dedicated audit connection — do not reuse any cursor from the

                            # surrounding try-block scope which may be in an uncertain state.

                            _mf_audit_conn = get_connection()

                            _mf_audit_cur = _mf_audit_conn.cursor()

                            try:

                                _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                                _flatten_order_result = ex_heal.create_order(

                                    symbol=_ccxt_pair, type='market', side=flatten_side, amount=phys_qty_abs,

                                    params=_safe_order_params({'reduceOnly': True}, _is_testnet),

                                    emergency=True,

                                    _audit_cursor=_mf_audit_cur,

                                    _call_site="resolve_net_mismatch._market_flatten:2507",

                                )

                                _mf_audit_conn.commit()

                                logger.info(f"✅ [MARKET-FLATTEN] Physical {flatten_side.upper()} order executed for {phys_qty_abs:.6f} {pair_normalized}.")

                                flatten_executed = True

                            except Exception as _fe:

                                if '-4061' in str(_fe) or 'positionside' in str(_fe).lower():

                                    try:

                                        ex_heal.create_order(

                                            symbol=_ccxt_pair, type='market',

                                            side=flatten_side, amount=phys_qty_abs,

                                            params={'reduceOnly': True},

                                            emergency=True,

                                            _audit_cursor=_mf_audit_cur,

                                            _call_site="resolve_net_mismatch._market_flatten_retry:2516",

                                        )

                                        _mf_audit_conn.commit()

                                        flatten_executed = True

                                    except Exception as _e:
                                        logger.debug(f'[EXPECTED] [MARKET-FLATTEN-RETRY] positionSide-stripped retry failed: {_e}')

                                if not flatten_executed:

                                    logger.error(f"❌ [MARKET-FLATTEN] Physical execution failed for {pair_normalized}: {_fe}")

                                # 🚀 RACE-CONDITION GUARD [V1.7.2]:

                                # -2022 ReduceOnly rejected has two causes in one-way mode:

                                # (a) Position flipped direction since snapshot → gap naturally resolved.

                                # (b) Bot's own TP limit order already covers the full position,

                                #     so adding another reduceOnly SELL exceeds the position size.
                                # In both cases, the gap is self-healing — don't block with REQUIRE_MANUAL.

                                if '-2022' in str(_fe) or 'reduceonly' in str(_fe).lower() or 'reduce_only' in str(_fe).lower():

                                    try:

                                        fresh_positions = ex_heal.fetch_positions() or []

                                        _ccxt_pair = _to_ccxt_pair(pair_normalized, _all_pairs)

                                        fresh_orders   = ex_heal.fetch_open_orders(_ccxt_pair) or []

                                        fresh_signed_qty = sum(

                                            float(p.get('contracts', 0))

                                            for p in fresh_positions

                                            if normalize_symbol(p.get('symbol', '')) == pair_normalized

                                        )

                                        # How much existing reduce capacity is already pending?

                                        # FIX #4 [V2.4.1]: Only count THIS BOT'S CQB_ TP orders,

                                        # not ALL open orders on the pair. Previously, any open buy/sell

                                        # orders from other bots could inflate pending_reduce_qty and

                                        # cause a false RACE-HEAL, masking a real offline fill gap.

                                        reduce_side = 'sell' if gap_qty_signed > 0 else 'buy'

                                        # We can identify our own bots' TP orders by CQB_ prefix in clientOrderId

                                        _our_bot_ids = [str(b.bot_id) for b in suspects]

                                        pending_reduce_qty = sum(

                                            float(o.get('amount', o.get('origQty', 0)))

                                            for o in fresh_orders

                                            if str(o.get('side', '')).lower() == reduce_side

                                            and (

                                                # Accept if it's one of our bots' TP/SL orders

                                                any(

                                                    str(o.get('clientOrderId', '')).startswith(f'CQB_{bid}_TP_') or

                                                    str(o.get('clientOrderId', '')).startswith(f'CQB_{bid}_SL_') or

                                                    str(o.get('clientOrderId', '')).startswith(f'CQB_{bid}_CLOSE_') or

                                                    str(o.get('clientOrderId', '')).startswith(f'CQB_{bid}_FLATTEN_CLOSE_')

                                                    for bid in _our_bot_ids

                                                )

                                                # Or if no clientOrderId and reduceOnly is explicitly set

                                                or (not o.get('clientOrderId') and o.get('reduceOnly', False))

                                            )

                                        )

                                        # Gap is healed if: (a) position now near target parity (0)

                                        #                  (b) pending TP already covers position

                                        fresh_gap = abs(fresh_signed_qty)

                                        position_covered_by_tp = pending_reduce_qty >= abs(fresh_signed_qty) - 0.0001

                                        if fresh_gap < 0.001 or position_covered_by_tp:

                                            logger.info(

                                                f"⚡ [RACE-HEAL] {pair_normalized}: Gap self-healed. "

                                                f"Fresh pos={fresh_signed_qty:.6f}, pending_reduce={pending_reduce_qty:.6f}. Skipping REQUIRE_MANUAL."

                                            )

                                            flatten_executed = True  # Position managed by bot's TP

                                    except Exception as _rfe:

                                        logger.warning(f"⚠️ [RACE-HEAL] Re-fetch failed: {_rfe}")





                    if flatten_executed or not ex_heal:

                        # Success or Offline: Zero out all local ledger tracks for this direction

                        for b in suspects:

                            # Abort wipe if this bot has a hedge child that is currently in trade
                            child_in_trade = False
                            try:
                                child_row = cursor.execute(
                                    "SELECT status FROM bots WHERE parent_bot_id=? AND bot_type='hedge_child'",
                                    (b.bot_id,)
                                ).fetchone()
                                if child_row and child_row[0] == 'IN TRADE':
                                    child_in_trade = True
                            except Exception:
                                pass

                            if not flatten_executed and child_in_trade:

                                logger.warning(f"🛡️ [WIPE-ABORT] Bot {b.name} (ID {b.bot_id}) has ACTIVE HEDGE CHILD (IN TRADE). Skipping wipe to protect hedge-exit math.")

                                continue



                            logger.info(f"💣 [WIPE] Zero-clearing memory for Bot {b.name} (ID {b.bot_id}) after market flatten.")

                            # ── FLATTEN FILL RECEIPT [v1.0] ───────────────────────────────────────
                            # Write a bot_orders row for the exchange fill so the ledger has an
                            # auditable record and open_qty decrements cleanly before safe_wipe_bot.
                            # This is the fix for post-flatten ledger corruption (no credit_fill path).
                            if b.in_trade:
                                try:
                                    from engine.ledger import credit_fill
                                    _fo = _flatten_order_result if isinstance(_flatten_order_result, dict) else {}
                                    _fill_price  = float(_fo.get('average') or _fo.get('price') or b.avg_entry_price or 0)
                                    _fill_qty    = float(_fo.get('filled') or _fo.get('amount') or phys_qty_abs)
                                    _fill_oid    = str(_fo.get('id') or f'FLATTEN_{b.bot_id}_{int(time.time())}')
                                    _fill_cid    = f"CQB_{b.bot_id}_FLATTEN_{b.cycle_id}_{int(time.time())}"
                                    save_bot_order(
                                        bot_id=b.bot_id,
                                        order_type='close',
                                        exchange_order_id=_fill_oid,
                                        price=_fill_price,
                                        amount=_fill_qty,
                                        step=b.current_step,
                                        status='open',   # credit_fill will set 'filled' + decrement open_qty
                                        client_order_id=_fill_cid,
                                        notes=f'Global flatten: reconciler market close of {phys_qty_abs:.6f} {pair_normalized}',
                                        cycle_id=b.cycle_id,
                                        position_side=None,  # BOTH → passes side-lock guard
                                    )
                                    credit_fill(
                                        bot_id=b.bot_id,
                                        order_id=_fill_cid,
                                        cumulative_qty=_fill_qty,
                                        avg_price=_fill_price,
                                        order_type='close',
                                        is_cumulative=True,
                                    )
                                    logger.info(
                                        f"✅ [MARKET-FLATTEN] Recorded fill in bot_orders and credited ledger "
                                        f"for bot {b.bot_id} (order_id={_fill_oid}, qty={_fill_qty:.6f} @ {_fill_price:.4f})"
                                    )
                                except Exception as _receipt_err:
                                    logger.error(
                                        f"❌ [MARKET-FLATTEN] Failed to write fill receipt for bot {b.bot_id}: {_receipt_err}"
                                    )
                            # ─────────────────────────────────────────────────────────────────────

                            self._execute_accounting_adjustment(b, 0.0, 0.0, "Ultimate Flatten Protocol: Zero-Parity Reset")

                            safe_wipe_bot(

                                b.bot_id, b.pair, b.direction,

                                reason=f"MARKET_FLATTEN: Ultimate Zero-Parity Protocol invoked.",

                                exit_price=0.0,

                                force=True,

                                human_approved=True,

                                cursor=cursor

                            )

                            # 🚀 Per-bot result so callers can identify which bots were cleared

                            results.append(ReconciliationResult(

                                bot_id=b.bot_id, bot_name=b.name, pair=pair_normalized,

                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,

                                details=f"Ultimate Flatten: Bot {b.name} virtual state wiped. Physical gap={phys_qty_abs:.6f}.",

                                requires_manual_intervention=False

                            ))



                    else:

                        # Only block if API call still failed after re-check

                        results.append(ReconciliationResult(

                            bot_id=0, bot_name="NET-GAP", pair=pair_normalized,

                            action_taken=ReconciliationAction.REQUIRE_MANUAL,

                            details=f"Market Flatten FAILED due to API error. Physical position still exists. Intervention required.",

                            requires_manual_intervention=True

                        ))



        return results



    def _find_proof_of_exit(self, bot: BotState, exchange: Any = None) -> Optional[Dict]:

        """

        Searches exchange history for STRICT PROOF that the bot's position was closed.

        Only CQB_ internally signed orders (TP/SL) matching the current cycle are accepted.

        No heuristic matching of random or manual trades is permitted (Proof-Only Protocol).

        """

        exchanges_to_check = [exchange] if exchange else [ex for ex in self.exchanges.values() if ex]

        

        for ex in exchanges_to_check:

            if not ex: continue

            try:

                # ONLY Check internally signed orders in fetch_closed_orders

                history = ex.fetch_closed_orders(bot.pair, limit=1000)

                if isinstance(history, list):

                    conn = get_connection()

                    cur = conn.cursor()

                    expected_cycle = bot.cycle_id

                    

                    for order in history:

                        cid = order.get('clientOrderId', '')

                        if cid.startswith(f"CQB_{bot.bot_id}_TP_") or cid.startswith(f"CQB_{bot.bot_id}_SL_"):

                            if order.get('status') in ['closed', 'filled']:

                                cur.execute("SELECT cycle_id, status FROM bot_orders WHERE order_id=? OR client_order_id=?", 

                                            (order.get('id'), cid))

                                row = cur.fetchone()

                                if row:

                                    db_cycle, db_status = row[0], row[1]

                                    if db_cycle == expected_cycle and db_status not in ['reset_cleared', 'auto_closed']:

                                        # Get the latest entry/grid fill timestamp for this bot and cycle

                                        cur.execute(

                                            "SELECT MAX(COALESCE(filled_at, updated_at, created_at)) FROM bot_orders "

                                            "WHERE bot_id = ? AND cycle_id = ? AND order_type IN ('entry', 'grid') AND status IN ('filled', 'reset_cleared')",

                                            (bot.bot_id, expected_cycle)

                                        )

                                        max_entry_ts = cur.fetchone()[0] or 0



                                        # Convert closed order fill timestamp from exchange (ms) to seconds

                                        order_fill_ts = int((order.get('lastTradeTimestamp') or order.get('timestamp') or 0) / 1000)



                                        if max_entry_ts > 0 and order_fill_ts > 0 and order_fill_ts < max_entry_ts:

                                            logger.warning(

                                                f"[PROOF-WALL-GUARD] Skipping exit proof order {order.get('id')} (CID: {cid}) "

                                                f"for bot {bot.bot_id}: order filled at {order_fill_ts} is BEFORE latest entry/grid fill {max_entry_ts}."

                                            )

                                            continue



                                        pass # conn.close() disabled for singleton safety

                                        return order

                    pass # conn.close() disabled for singleton safety

                        

            except Exception as e:

                logger.error(f"Failed to fetch exit proof for Bot {bot.bot_id}: {e}")

        return None



    def is_sole_bot(self, pair: str, bots: List[Any]) -> bool:

        """

        Returns True if there is only ONE active bot for this pair.

        Used to increase confidence in physical position adoption.

        """

        norm_target = normalize_symbol(pair)

        active_pair_bots = [b for b in bots if b.is_active and normalize_symbol(b.pair) == norm_target]

        return len(active_pair_bots) == 1



    def _verify_entry_existence(self, bot: BotState, exchange: Any = None) -> bool:

        """

        Verifies if the bot's supposed Entry Order actually exists and is filled.

        Returns True if valid entry found, False if missing/cancelled (Phantom).

        """

        if not bot.entry_order_id:

            return False # No Order ID = Phantom State



        exchanges_to_check = [exchange] if exchange else [ex for ex in self.exchanges.values() if ex]

        for ex in exchanges_to_check:

            if not ex: continue

            try:

                # We need to use the exchange interface wrapper

                if bot.entry_order_id and any(str(bot.entry_order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                    logger.info(f"🔎 [PHANTOM-CHECK] Skipping fetch_order for synthetic entry ID {bot.entry_order_id}")
                    order = None
                else:
                    order = ex.fetch_order(bot.entry_order_id, bot.pair)

                if order:

                    status = order.get('status', '').lower()

                    if status in ['filled', 'closed']:

                        return True

                    if status in ['canceled', 'rejected', 'expired']:

                        return False # Explicitly failed

                

            except Exception as e:

                logger.warning(f"Entry verification failed for {bot.entry_order_id}: {e}")

                # If 404/Not Found, usually raises exception. 

                # CCXT usually raises OrderNotFound.

                if "OrderNotFound" in str(e) or "not found" in str(e).lower():

                    return False

        

        # If we checked all exchanges and found nothing... assume False (Phantom)?

        # Or be conservative?

        # If ID format looks real, returning False is checking "Reality".

        return False



    def _lock_irreconcilable_bot(self, bot: BotState):

        """NEUTERED: Warn only — never deactivate bots automatically."""

        logger.warning(

            f"⚠️ Bot {bot.name} (ID {bot.bot_id}) has irreconcilable state. "

            f"Flagging only — NO LOCK, NO DEACTIVATION."

        )





    # ------------------------------------------------------------------

    # MAIN ENTRY POINT

    # ------------------------------------------------------------------

    def reconcile_all(self, force_adoption: bool = False):
        from engine.write_queue import WriteQueue
        return WriteQueue().put_and_wait(self._reconcile_all_internal, force_adoption, _wq_timeout=120.0)

    def _reconcile_all_internal(self, force_adoption: bool = False):

        logger.info("🔄 STARTING RECONCILIATION CYCLE")

        

        # 0. Phantom Entry Cleanup (FIX #2)

        # Detects bots with total_invested > 0 but entry_confirmed=0 and avg_entry=0

        # These are phantom entries that were never actually filled.

        try:

            self._cleanup_phantom_entries()

        except Exception as e:

            logger.error(f"Phantom entry cleanup failed: {e}")

        

        # 1. Offline Fills (Updates DB with latest 'bot_orders')

        self.reconstruct_offline_fills()



        # 1.5 Rigorous Trade Memory Alignment (DNA Sync)

        # Ensure 'trades' memory matches 'bot_orders' ledger for ALL bots AFTER offline fills are applied.

        self._align_memory_to_ledger()

        # 1.6 Auto-Clear Stale REQUIRE_MANUAL_PROOF Gates
        try:
            from engine.database import get_connection, get_pair_virtual_net
            from engine.parity_gates import get_exchange_signed_net, qty_tolerance
            _conn = get_connection()
            _ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)
            if _ex:
                _gated_bots = _conn.execute(
                    "SELECT id, name, pair, bot_type FROM bots WHERE status = 'REQUIRE_MANUAL_PROOF'"
                ).fetchall()
                for _gated_id, _gated_name, _gated_pair, _gated_type in _gated_bots:
                    virtual = get_pair_virtual_net(_gated_pair)
                    physical = get_exchange_signed_net(_ex, _gated_pair)
                    if physical is not None:
                        tol = qty_tolerance()
                        if abs(virtual - physical) < tol:
                            # Determine correct clear status
                            _trade_row = _conn.execute(
                                "SELECT open_qty FROM trades WHERE bot_id = ?", (_gated_id,)
                            ).fetchone()
                            _has_qty = (_trade_row[0] > 0.0001) if (_trade_row and _trade_row[0] is not None) else False
                            
                            if _has_qty:
                                target_status = 'IN TRADE'
                            else:
                                target_status = 'Scanning' if _gated_type == 'standard' else 'hedge_standby'
                                
                            logger.info(
                                f"[AUTO-CLEAR] Bot {_gated_id} ({_gated_name}): pair net now "
                                f"matches exchange (virtual={virtual}, physical={physical}). "
                                f"Clearing REQUIRE_MANUAL_PROOF -> {target_status}."
                            )
                            _conn.execute(
                                "UPDATE bots SET status=? WHERE id=?", (target_status, _gated_id)
                            )
                            _conn.commit()
        except Exception as _clear_err:
            logger.error(f"❌ [AUTO-CLEAR-MANUAL-PROOF] Failed: {_clear_err}")


        # INV-26: Per-bot ghost check (all active bots)

        try:

            from engine.oneway_netting import detect_bot_ghost, wipe_bot_ghost

            from engine.database import get_connection

            _conn = get_connection()

            _ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)

            if _ex:

                _active_bots = _conn.execute(

                    "SELECT id FROM bots WHERE is_active=1"

                ).fetchall()

                for (_bot_id,) in _active_bots:

                    if detect_bot_ghost(_ex, _bot_id, _conn):

                        wipe_bot_ghost(_ex, _bot_id, _conn)

        except Exception as _ghost_err:

            logger.error(f"❌ [RECON-BOT-GHOST] Failed check: {_ghost_err}")

        

        # 2. Fetch Fresh State

        bot_states = self.get_bot_states()

        success, all_positions = self.fetch_all_exchange_positions()

        

        results = []

        if not success:

            logger.error("🛑 [RECON-ABORT] Exchange position fetch failed. Skipping net-sum check to prevent false resets.")

            return results



        # Flatten orders

        all_pairs = list(set([b.pair for b in bot_states]))

        all_orders = self.fetch_all_exchange_orders(all_pairs)

        

        results = []

        

        # 3. Individual Bot Validation (Zombies)

        zombie_results = self.validate_individual_bots(bot_states, all_orders)

        results.extend(zombie_results)

        # 4. Global Net Check & Resolution (now includes Net-Sum ghost detection)
        net_results = self.resolve_net_mismatch(bot_states, all_positions, all_orders, force_adoption=force_adoption)
        results.extend(net_results)

        # Phase B: Sync pair to exchange during reconciler cycle
        try:
            from engine.oneway_netting import sync_pair_to_exchange, detect_unowned_exchange_positions
            from engine.database import get_connection
            from config.settings import config as _pa_cfg
            _conn = get_connection()
            _ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)
            if _ex:
                # Query all active pairs
                _active_pairs = [r[0] for r in _conn.execute("SELECT DISTINCT pair FROM bots WHERE is_active=1").fetchall()]
                for _pair in _active_pairs:
                    sync_pair_to_exchange(_pair, _ex, _conn)
                # Run auto-detection for unowned exchange positions
                detect_unowned_exchange_positions(_conn, _ex)

        except Exception as _sync_err:
            logger.error(f"❌ [RECON-EXCHANGE-SYNC] Failed exchange sync check: {_sync_err}")

        logger.info(f"✅ RECONCILIATION COMPLETE. {len(results)} actions taken.")

        return results



    def _cleanup_phantom_entries(self):

        """

        FIX #2: Detects and resets phantom entries.

        A phantom entry has total_invested > 0 but entry_confirmed = 0 AND avg_entry_price = 0.

        This means an entry order was recorded in DB but never actually filled.

        """

        conn = get_connection()

        cursor = conn.cursor()

        cursor.execute("""

            SELECT t.bot_id, b.name, b.pair, t.total_invested

            FROM trades t

            JOIN bots b ON t.bot_id = b.id

            WHERE t.total_invested > 0 

              AND t.entry_confirmed = 0 

              AND (t.avg_entry_price IS NULL OR t.avg_entry_price = 0)

        """)

        phantoms = cursor.fetchall()

        

        for bot_id, bot_name, pair, invested in phantoms:

            logger.warning(

                f"👻 [PHANTOM-ENTRY] Bot {bot_name} (ID {bot_id}): "

                f"invested=${invested:.2f} but entry_confirmed=0 and avg_entry=0. Auto-resetting."

            )

            # 🚀 ARCHITECTURAL GATE (v2.3.7):

            # Never manually wipe trades. Use safe_wipe_bot to ensure the ledger

            # is correctly marked with 'reset_cleared'.

            safe_wipe_bot(bot_id, pair, "LONG" if "LONG" in bot_name.upper() else "SHORT", "RESET_PHANTOM_ENTRY", force=True, human_approved=True, cursor=cursor)

            

            from .database import log_reconciliation

            log_reconciliation(

                bot_id=bot_id,

                pair=pair,

                action="RESET_PHANTOM_ENTRY",

                details=f"Phantom entry reset: invested=${invested:.2f}, confirmed=0, avg_entry=0"

            )

            log_trade(

                bot_id=bot_id, action='PHANTOM_RESET', symbol=pair,

                price=0, amount=0, cost_usdc=0,

                order_id=f"PHANTOM_{int(time.time())}",

                step=0, notes="Auto-reset phantom entry (never filled)", pnl=0

            )

        

        if phantoms:

            conn.commit()

            logger.info(f"✅ Cleaned up {len(phantoms)} phantom entries.")

        pass # conn.close() disabled for singleton safety



    @staticmethod

    def _compute_step_from_invested(total_invested: float, base_size: float, multiplier: float) -> int:

        """

        📐 MATHEMATICAL STEP DEDUCTION.

        Reverse-engineers the exact martingale step from total_invested using the

        geometric series formula without heuristic guessing.



        Geometric series: total = base * (1 + m + m^2 + ... + m^(n-1))

          where n = number of steps completed, m = martingale_multiplier



        Special case m == 1: total = base * n  →  n = round(total / base)



        For m != 1:  total = base * (m^n - 1) / (m - 1)

          Solving for n: n = log(total*(m-1)/base + 1) / log(m)



        Fallback: if config is missing or degenerate, returns max(1, fallback).

        """

        import math



        # Guard: need valid config to do real math

        if base_size <= 0:

            logger.warning(f"⚠️ [STEP-PROOF] base_size={base_size} is invalid — cannot compute step. Defaulting to 1.")

            return 1



        try:

            if abs(multiplier - 1.0) < 1e-6:

                # Linear case: total = base * n

                n = total_invested / base_size

            else:

                # Geometric case: total = base * (m^n - 1) / (m - 1)

                # → m^n = total * (m - 1) / base + 1

                # → n   = log(total*(m-1)/base + 1) / log(m)

                ratio = total_invested * (multiplier - 1.0) / base_size + 1.0

                if ratio <= 0:

                    logger.warning(f"⚠️ [STEP-PROOF] ratio={ratio:.4f} is non-positive — defaulting to 1.")

                    return 1

                n = math.log(ratio) / math.log(multiplier)



            # Round to nearest integer; clamp to [1, 20]

            proven = max(1, min(20, round(n)))

            return proven

        except (ValueError, ZeroDivisionError, OverflowError) as e:

            logger.warning(f"⚠️ [STEP-PROOF] Math error computing step: {e}. Defaulting to 1.")

            return 1



    def _execute_accounting_adjustment(self, bot: BotState, phys_notional: float, phys_qty: float, reason: str):

        """

        🚀 PHYSICAL ADOPTION:

        Proof-Only v3.0: Synthetic adoptions are DISABLED.

        This function now only supports Zero-Parity Resets (phys_qty=0.0).

        For all other mismatches, it reports a proof violation.

        """

        from .database import get_connection, log_reconciliation

        try:

            if abs(phys_qty) > 1e-9:

                logger.error(f"❌ [PROOF-VIOLATION] {bot.name}: Physical mismatch (Phys={phys_qty:.6f}) detected but NO synthetic adoption is allowed. Intervention required.")

                return False



            # If we are here and phys_qty=0, it's a legitimate zero-parity wipe.

            logger.info(f"💣 [WIPE-EXECUTE] {bot.name}: Executing zero-parity reset. Reason: {reason}")

            return True



        except Exception as e:

            logger.error(f"[RECON-WIPE] Failed for bot {bot.name}: {e}", exc_info=True)

            return False







    def _fix_ghost_bot(self, bot: BotState, proof_order_id: str = "MANUAL_FIX"):
        from engine.write_queue import WriteQueue
        return WriteQueue().put_and_wait(self._fix_ghost_bot_internal, bot, proof_order_id, _wq_timeout=120.0)

    def _fix_ghost_bot_internal(self, bot: BotState, proof_order_id: str = "MANUAL_FIX"):

        """

        Resets a ghost bot to IDLE state.

        This is used when a bot claims a position but verification proves it doesn't exist.

        """

        try:

            logger.info(f"🔧 Fixing Ghost Bot {bot.name} (ID {bot.bot_id})...")

            

            # Log the fix to trade history

            log_trade(

                bot_id=bot.bot_id,

                action='OFFLINE_SYNC' if str(proof_order_id).isdigit() else 'PHANTOM_RESET',

                symbol=bot.pair,

                price=0,

                amount=0,

                cost_usdc=0,

                order_id=f"GHOST_{int(time.time())}",

                step=bot.current_step,

                notes=f"System reset: {proof_order_id}",

                pnl=0

            )

            

            # Reset State in DB

            conn = get_connection()

            cursor = conn.cursor()

            cursor.execute("""

                UPDATE trades 

                SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=?,

                    avg_entry_price=0, target_tp_price=0

                WHERE bot_id=?

            """, (int(time.time()), bot.bot_id))  # 🚀 FUNDAMENTAL FIX: Never set basket_start_time=0. Always stamp reset time so EE fires on next cycle.

            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot.bot_id,))

            

            # Also cancel any open internal orders and archive filled orders to prevent zombie revival

            cursor.execute("UPDATE bot_orders SET status='cancelled' WHERE bot_id=? AND status IN ('open', 'new', 'placing', 'cancelling')", (bot.bot_id,))

            cursor.execute("UPDATE bot_orders SET status='reset_cleared' WHERE bot_id=? AND (status NOT IN ('open', 'new', 'placing', 'cancelling', 'auto_closed', 'reset_cleared', 'cancelled') OR (status IN ('cancelled', 'canceled') AND filled_amount > 0))", (bot.bot_id,))

            

            conn.commit()

            pass # conn.close() disabled for singleton safety

            logger.info(f"✅ Ghost Bot {bot.name} successfully reset to Scanning.")

            

        except Exception as e:

            logger.error(f"Failed to fix ghost bot {bot.name}: {e}")



    # ------------------------------------------------------------------

    # HELPER METHODS (Preserved/Adapted)

    # ------------------------------------------------------------------

    def fetch_all_exchange_positions(self) -> Tuple[bool, Dict[str, List[ExchangePosition]]]:

        all_pos = {}

        success = True

        seen_positions = set()

        for mt, ex in self.exchanges.items():

            if not ex: continue

            try:

                raw = ex.fetch_positions()

                if not isinstance(raw, list):

                    logger.warning(f"⚠️ fetch_positions returned non-list for exchange '{mt}': {raw}. Aborting to prevent false wipes.")

                    success = False

                    continue

                for p in raw:

                    sym = normalize_symbol(p.get('symbol', ''))

                    

                    # Determine actual direction for this entry.

                    # ⚠️ CRITICAL FIX: Binance one-way mode returns contracts as UNSIGNED (always > 0).

                    # We MUST use info.positionSide (LONG/SHORT/BOTH) first.

                    # For one-way positions (positionSide='BOTH'), use the SIGNED positionAmt to determine direction.

                    raw_info = p.get('info', {})

                    position_side = str(raw_info.get('positionSide', '')).upper()



                    if position_side in ('LONG', 'SHORT'):

                        # Hedge mode — positionSide is explicit

                        raw_size = float(p.get('contracts', 0) or 0)

                        float_size = raw_size

                        side = position_side

                    else:

                        # One-way mode (positionSide='BOTH' or missing) — use signed positionAmt

                        raw_pos_amt = raw_info.get('positionAmt', None)

                        if raw_pos_amt is not None:

                            float_size = float(raw_pos_amt)

                        else:

                            float_size = float(p.get('contracts', 0) or p.get('size', 0) or 0)

                        side = 'LONG' if float_size > 0 else ('SHORT' if float_size < 0 else 'FLAT')

                    

                    float_size = abs(float_size)  # Always store size as positive, side carries the direction

                    

                    # Deduplicate globally using symbol and side to prevent double-counting if endpoints overlap

                    pos_id = f"{sym}_{side}"

                    if pos_id in seen_positions:

                        continue

                    seen_positions.add(pos_id)

                    

                    pos = ExchangePosition(

                        symbol=sym,

                        side=side,

                        size=abs(float_size),

                        entry_price=float(p.get('entryPrice',0) or 0.0),

                        mark_price=float(p.get('markPrice',0)),

                        unrealized_pnl=float(p.get('unrealizedPnl',0))

                    )

                    if sym not in all_pos: all_pos[sym] = []

                    all_pos[sym].append(pos)

            except Exception as e:

                logger.error(f"Pos fetch failed {mt}: {e}")

                success = False

        return success, all_pos



    def fetch_all_exchange_orders(self, pairs: List[str]) -> Dict[str, List[ExchangeOrder]]:

        orders_by_pair = {}

        seen_order_ids = set() # Prevent duplicated orders from overlapping API endpoints

        for pair in pairs:

            norm_pair = normalize_symbol(pair)

            if norm_pair not in orders_by_pair:

                orders_by_pair[norm_pair] = []

            for mt, ex in self.exchanges.items():

                if not ex: continue

                try:

                    raw = ex.fetch_open_orders(pair)

                    if not isinstance(raw, list):

                        logger.warning(f"⚠️ fetch_open_orders returned non-list for exchange '{mt}', pair '{pair}': {raw}. Skipping.")

                        continue

                    for o in raw:

                        oid = str(o.get('id', ''))

                        if oid in seen_order_ids:

                            continue

                        seen_order_ids.add(oid)

                        

                        orders_by_pair[norm_pair].append(ExchangeOrder(

                            order_id=oid,

                            symbol=pair,

                            side=o.get('side',''),

                            order_type=o.get('type','limit'),

                            price=float(o.get('price',0) or 0),

                            amount=float(o.get('amount',0) or 0),

                            status=o.get('status','open'),

                            client_order_id=o.get('clientOrderId')

                        ))

                except Exception as _e:
                    logger.warning(f'[GET-OPEN-ORDERS] {pair} fetch failed (skipping pair): {_e}')

        return orders_by_pair



    def get_bot_states(self) -> List[BotState]:

        bots = get_all_bots()

        states = []

        conn = get_connection()

        cursor = conn.cursor()

        

        for b in bots:

            bot_id, name, pair, is_active = b[0], b[1], b[2], b[3]

            logger.debug(f"🔍 [GET-STATE] Examining Bot {bot_id} (Active={is_active})")

            

            # The get_bot_status function is imported from .database,

            # so we assume it's modified to return 'cycle_id' at index 13.

            # The instruction implies modifying the *source* of get_bot_status.

            # For this file, we just use the updated return value.

            status = get_bot_status(bot_id)

            if not status: 

                logger.warning(f"⚠️ [GET-STATE] No status found for Bot {bot_id}")

                continue

            

            # Read the actual entry_confirmed flag from the database

            order_ids = get_bot_order_ids(bot_id)

            confirmed = bool(status.get('entry_confirmed', False))

            

            # Load bot config for mathematical step deduction
            cursor.execute("SELECT base_size, martingale_multiplier, parent_bot_id, hedge_child_bot_id FROM bots WHERE id=?", (bot_id,))
            cfg_row = cursor.fetchone()
            base_size = float(cfg_row[0] or 0.0) if cfg_row else 0.0
            mart_mult = float(cfg_row[1] or 1.0) if cfg_row else 1.0
            parent_bot_id = int(cfg_row[2]) if cfg_row and cfg_row[2] is not None else None
            hedge_child_bot_id = int(cfg_row[3]) if cfg_row and cfg_row[3] is not None else None

            states.append(BotState(
                bot_id=bot_id,
                name=name,
                pair=pair,
                direction=status['direction'],
                is_active=bool(is_active),
                in_trade=status['total_invested'] > 0,
                total_invested=status['total_invested'],
                avg_entry_price=status['avg_entry_price'],
                target_tp_price=status['target_tp_price'],
                current_step=status['current_step'],
                basket_start_time=status['basket_start_time'],
                entry_order_id=str(order_ids.get('entry_order_id')) if order_ids.get('entry_order_id') else None,
                tp_order_id=str(order_ids.get('tp_order_id')) if order_ids.get('tp_order_id') else None,
                has_confirmed_entry=confirmed,
                cycle_id=status.get('cycle_id', 1),
                cycle_start_time=status.get('cycle_start_time', 0),
                base_size=base_size,
                martingale_multiplier=mart_mult,
                bot_status=status.get('status', ''),
                cycle_phase=status.get('cycle_phase', 'IDLE'),
                parent_bot_id=parent_bot_id,
                hedge_child_bot_id=hedge_child_bot_id,
            ))

        pass # conn.close() disabled for singleton safety

        return states



    def _align_memory_to_ledger(self):
        from engine.write_queue import WriteQueue
        return WriteQueue().put_and_wait(self._align_memory_to_ledger_internal, _wq_timeout=120.0)

    def _align_memory_to_ledger_internal(self):

        """

        RIGOROUS MEMORY ALIGNMENT:

        Enforces that the 'trades' table (total_invested, avg_entry) is exactly 

        subordinate to the 'bot_orders' ledger (filled entries - filled exits).

        If memory deviates from DNA, memory is overwritten.

        """

        conn = get_connection()

        cursor = conn.cursor()

        try:

            # 🚀 HEDGE-MODE FIX: Align memory using side-aware filtering

            cursor.execute("""

                SELECT b.id, b.name, t.cycle_id, t.total_invested, b.pair, t.entry_confirmed, t.position_side

                FROM bots b

                LEFT JOIN trades t ON b.id = t.bot_id

                WHERE b.is_active = 1

            """)

            active_bots = cursor.fetchall()



            for b_id, b_name, cycle, db_inv, pair, entry_confirmed, b_side in active_bots:

                self.heal_cycle_fragmentation(b_id, cycle)

                from .database import sync_trades_from_orders

                sync_trades_from_orders(b_id)

                

                from engine.database import recompute_invested_from_orders

                

                true_cost, true_avg_price, true_qty, true_step = recompute_invested_from_orders(b_id)

                true_inv = true_cost

                avg_entry = true_avg_price

                ledger_qty = true_qty



                # 🚀 STEP IDENTITY GUARD: Always align step to recomputed ledger truth

                cursor.execute("UPDATE trades SET current_step = ? WHERE bot_id = ?", (true_step, b_id))



                if abs(db_inv - true_inv) > 0.01:

                    logger.warning(f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}) memory=${db_inv:.4f} vs DNA-Ledger=${true_inv:.4f}.")

                    if ledger_qty <= 0.0001:

                        # Always check physical reality regardless of entry_confirmed

                        norm_pair = pair.split(':')[0].replace('/', '')

                        cursor.execute("SELECT direction FROM bots WHERE id=?", (b_id,))

                        b_dir_row = cursor.fetchone()

                        phys_qty = 0.0

                        if b_dir_row:

                            phys_snap = cursor.execute(

                                "SELECT ABS(size) FROM active_positions WHERE pair=? AND side=?",

                                (norm_pair, 'LONG' if b_dir_row[0].upper() == 'LONG' else 'SHORT')

                            ).fetchone()

                            phys_qty = float(phys_snap[0]) if phys_snap and phys_snap[0] else 0.0



                        # v3.0.5: If snapshot says 0 but DB shows invested cost, the snapshot is

                        # stale. Do a live exchange fetch before trusting the cached table.

                        # active_positions refreshes every 60 cycles (~5 min); a fresh fill

                        # will not be there yet. This is the last guard before an irreversible wipe.

                        if phys_qty < 0.0001 and db_inv > 0.01:

                            try:

                                _ex = list(self.exchanges.values())[0] if self.exchanges else None

                                if _ex:

                                    _bot_dir = b_dir_row[0].upper() if b_dir_row else 'LONG'

                                    for _lp in (_ex.fetch_positions() or []):

                                        _lsym = _lp.get('symbol', '').replace('/', '').split(':')[0].upper()

                                        _lside = str(_lp.get('side', '')).upper()

                                        _lqty = float(_lp.get('contracts') or 0)

                                        if _lsym == norm_pair and _lside == _bot_dir and abs(_lqty) > 0.0001:

                                            phys_qty = abs(_lqty)

                                            logger.info(

                                                f"[DNA-LIVE-CHECK] Bot {b_id} ({b_name}): "

                                                f"Snapshot stale (0), live exchange shows {phys_qty:.6f}. "

                                                f"Preserving state."

                                            )

                                            break

                            except Exception as _live_err:

                                logger.warning(f"[DNA-ALIGN] Live fetch failed for {b_name}: {_live_err}")



                        if phys_qty > 0.0001:

                            logger.warning(

                                f"[DNA-PROTECT] Bot {b_id} ({b_name}): Ledger empty but exchange "

                                f"shows {phys_qty:.6f} units (snapshot or live). Preserving state."

                            )

                            continue  # Skip wipe logic below

                        

                        elif entry_confirmed:

                            # 🚀 FIX 2: IDLE ghost detection — if cycle_phase=IDLE and no physical

                            # position AND ledger is empty, safe to reset regardless of entry_confirmed.

                            phase_row = cursor.execute(

                                "SELECT COALESCE(cycle_phase, 'ACTIVE') FROM trades WHERE bot_id=?", (b_id,)

                            ).fetchone()

                            cycle_phase = (phase_row[0] if phase_row else 'ACTIVE') or 'ACTIVE'



                            if cycle_phase == 'IDLE':

                                logger.warning(

                                    f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}): IDLE + zero ledger + zero physical. Force-resetting phantom memory."

                                )

                                # Fetch bot direction for safe_wipe_bot

                                _dir_row = cursor.execute("SELECT direction FROM bots WHERE id=?", (b_id,)).fetchone()

                                _bot_direction = str(_dir_row[0]).upper() if _dir_row and _dir_row[0] else "LONG"

                                

                                # 🚀 ARCHITECTURAL GATE (v2.3.7): Use safe_wipe_bot

                                safe_wipe_bot(b_id, norm_pair, _bot_direction, "DNA_ALIGN_IDLE", human_approved=True, cursor=cursor)

                            else:

                                logger.warning(

                                    f"⚠️ [DNA-HOLD] Bot {b_id} ({b_name}): entry_confirmed=1, "

                                    f"phase={cycle_phase}, ledger qty=0 but not IDLE. Holding."

                                )

                        else:

                            # v3.0.5: Recency guard. If any fill landed in the last 5 min,

                            # the bot just entered and the ledger is still being sealed by the

                            # async DB worker. Deferring until the next alignment cycle (15 min)

                            # guarantees a fully-committed ledger before any wipe decision.

                            _recent_cutoff = int(time.time()) - 300

                            _recent_fills = cursor.execute(

                                "SELECT COUNT(*) FROM bot_orders WHERE bot_id=? "

                                "AND filled_amount > 0 AND (filled_at >= ? OR updated_at >= ?)",

                                (b_id, _recent_cutoff, _recent_cutoff)

                            ).fetchone()[0] or 0



                            if _recent_fills > 0:

                                logger.warning(

                                    f"[DNA-ALIGN-GRACE] Bot {b_id} ({b_name}): "

                                    f"{_recent_fills} fill(s) in last 5 min — ledger still settling. "

                                    f"Deferring wipe to next cycle."

                                )

                            else:

                                logger.warning(

                                    f"[DNA-ALIGN] Bot {b_id} ({b_name}): Ledger empty, "

                                    f"entry_confirmed=0, no recent fills. Resetting."

                                )

                                _dir_row = cursor.execute(

                                    "SELECT direction FROM bots WHERE id=?", (b_id,)

                                ).fetchone()

                                _bot_direction = str(_dir_row[0]).upper() if _dir_row and _dir_row[0] else "LONG"

                                safe_wipe_bot(b_id, norm_pair, _bot_direction, "DNA_ALIGN_RESET", human_approved=True, cursor=cursor)

                    else:

                        cursor.execute("UPDATE trades SET total_invested=?, avg_entry_price=? WHERE bot_id=?", (true_inv, avg_entry, b_id))

            conn.commit()

        except Exception as e:

            logger.error(f"❌ [DNA-ALIGN] Critical failure during alignment pass: {e}")

        finally:

            pass # conn.close() disabled for singleton safety



    def heal_cycle_fragmentation(self, bot_id: int, active_cycle: int):

        """

        CYCLE FRAGMENTATION HEALER:

        Assigns a cycle_id to confirmed fills (status='filled') that have a valid

        CQB_ client_order_id but are missing a cycle_id (NULL).



        🚀 FIX 4: NEVER migrate status='new'/'open' orders across cycles.

        These are standing limit orders on the exchange placed during a specific cycle.

        Their cycle_id is ground truth — migrating them corrupts the accounting of

        the cycle they actually belong to (caused SOL bot TP from cycle 1 to pollute cycle 2).



        The correct proof of ownership is embedded in client_order_id: CQB_{bot_id}_{type}_{ts}

        This is what must be used, not a numeric cycle comparison.

        """

        conn = get_connection()

        cursor = conn.cursor()

        try:

            if not active_cycle: active_cycle = 1

            # Only heal truly orphaned fills: filled rows with NULL cycle_id and a CQB proof ID.

            # Standing orders (new/open) keep their original cycle_id — they are live exchange orders.

            cursor.execute(

                "UPDATE bot_orders SET cycle_id = ? "

                "WHERE bot_id = ? AND cycle_id IS NULL "

                "AND status NOT IN ('reset_cleared', 'failed', 'cancelled', 'new', 'open', 'placed', 'placing', 'cancelling') "

                "AND client_order_id LIKE 'CQB_%'",

                (active_cycle, bot_id)

            )

            if cursor.rowcount > 0:

                logger.warning(

                    f"🩹 [RECON-HEAL] Assigned cycle_id={active_cycle} to {cursor.rowcount} "

                    f"NULL-cycle filled orders for Bot {bot_id} (CQB ID proof confirmed)."

                )

                conn.commit()

        except Exception as e:

            logger.error(f"Error healing cycle fragmentation for bot {bot_id}: {e}")




    def _heal_local_crosscycle_orphans(self, bot_id: int, current_cycle: int) -> int:
        """
        v4.1.5 — Cross-Cycle Orphan Healer.

        Before the exchange history API scan, checks local bot_orders for fills
        in cycles < current_cycle that have positive net qty (entries with no
        matching exit in the same cycle, still status='filled').  These are fills
        the bot acquired in an older cycle whose cycle_id != trades.cycle_id,
        making them invisible to the standard recompute_invested_from_orders call.

        Healing strategy (Approach B — non-mutating, per user decision):
            - Identify the lowest orphaned cycle_id (cycle_floor).
            - Call seal_trade_state(bot_id, force_recompute=True, cycle_floor=cycle_floor).
            - seal_trade_state threads cycle_floor through WriteQueue into
              _seal_trade_state_internal → recompute_invested_from_orders(cycle_floor=...).
            - recompute spans [cycle_floor, trades.cycle_id], making orphaned fills visible.
            - trades.open_qty / total_invested / avg_entry_price are corrected in a
              single authoritative write via the normal seal path.

        INV-19+ guarantee: bot_orders.cycle_id is NEVER mutated.  Historical fill
        attribution is preserved exactly as it was written at order creation.

        Returns count of orphaned cycles found and healed (0 if none).
        """
        conn = get_connection()
        cursor = conn.cursor()

        try:
            orphan_cycles = cursor.execute("""
                SELECT bo.cycle_id,
                       SUM(CASE WHEN bo.order_type IN
                               ('entry','grid','adoption','adoption_add','carry')
                               THEN bo.filled_amount ELSE 0 END) AS entry_qty,
                       SUM(CASE WHEN bo.order_type IN
                               ('tp','close','dust_close','sl','adoption_reduce','flatten_close')
                               THEN bo.filled_amount ELSE 0 END) AS exit_qty
                FROM bot_orders bo
                WHERE bo.bot_id = ?
                  AND bo.cycle_id < ?
                  AND bo.cycle_id IS NOT NULL
                  AND bo.filled_amount > 0
                  AND bo.status = 'filled'
                GROUP BY bo.cycle_id
                HAVING (entry_qty - exit_qty) > 1e-6
                ORDER BY bo.cycle_id ASC
            """, (bot_id, current_cycle)).fetchall()
        except Exception as _qe:
            logger.error(f"[CROSSCYCLE-HEAL] Bot {bot_id}: orphan scan query failed: {_qe}")
            return 0

        if not orphan_cycles:
            return 0

        lowest_orphan_cycle = orphan_cycles[0][0]
        total_orphan_qty = sum(abs(float(r[1]) - float(r[2])) for r in orphan_cycles)
        orphan_cycle_ids = [r[0] for r in orphan_cycles]

        # 🚀 [v4.1.5 CROSSCYCLE HEAL AVOID DUPLICATE]
        # Check current trades cache first to see if it is already healed.
        # If the trades table open_qty already matches the recomputed quantity with the cycle_floor,
        # we bypass calling seal_trade_state to avoid redundant DB writes/log pollution.
        try:
            curr_trade = cursor.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
            if curr_trade:
                curr_qty = float(curr_trade[0] or 0.0)
                from engine.database import recompute_invested_from_orders
                _, _, healed_qty, _ = recompute_invested_from_orders(bot_id, cycle_floor=lowest_orphan_cycle)
                if abs(curr_qty - healed_qty) < 1e-6:
                    return 0
        except Exception as _check_err:
            logger.warning(f"[CROSSCYCLE-HEAL] Bot {bot_id}: already-healed check failed: {_check_err}")

        logger.critical(
            f"[CROSSCYCLE-HEAL] ⚠️ Bot {bot_id}: {len(orphan_cycles)} cross-cycle orphan "
            f"cycle(s) detected: {orphan_cycle_ids}. "
            f"Total uncredited qty={total_orphan_qty:.6f}. "
            f"Healing via seal_trade_state(cycle_floor={lowest_orphan_cycle})."
        )

        try:
            from engine.ledger import seal_trade_state
            result = seal_trade_state(
                bot_id,
                force_recompute=True,
                cycle_floor=lowest_orphan_cycle,
            )
            if result:
                logger.info(
                    f"[CROSSCYCLE-HEAL] ✅ Bot {bot_id}: seal complete — "
                    f"open_qty={result.get('qty', '?'):.6f} "
                    f"cost={result.get('cost', '?'):.4f} "
                    f"avg={result.get('avg', '?'):.4f} "
                    f"step={result.get('step', '?')} "
                    f"(cycles {lowest_orphan_cycle}..{current_cycle} included)."
                )
            else:
                logger.warning(
                    f"[CROSSCYCLE-HEAL] Bot {bot_id}: seal_trade_state returned empty "
                    f"result. Orphan may not be fully healed — check SEAL logs."
                )
        except Exception as _seal_err:
            logger.error(
                f"[CROSSCYCLE-HEAL] Bot {bot_id}: seal_trade_state failed: {_seal_err}. "
                f"Orphan cycles {orphan_cycle_ids} remain unhealed."
            )
            return 0

        return len(orphan_cycles)

    def perform_forensic_reconstruction(self, pair: str) -> dict:


        """

        🚀 FORENSIC RECONSTRUCTION — Forced Ticker Deep-Scan.

        Specifically looks for proof-of-cycle (CQB_ prefixes) to adopt orphans.

        Called by UI when a user wants to adopt a manually identified orphan.

        """

        logger.info(f"🕵️‍♂️ [FORENSIC] Deep reconstruction scan triggered for {pair}.")

        

        # Reset cooldown for this specific pair to allow immediate scan

        pair_key = f'_last_pair_scan_{pair}'

        if hasattr(StateReconciler, pair_key):

            delattr(StateReconciler, pair_key)

            

        # Execute 7-day deep scan (168 hours)

        results = self.reconstruct_offline_fills(since_hours=168, pair_filter=pair)

        

        logger.info(f"🕵️‍♂️ [FORENSIC] Scan complete for {pair}. Recovery stats: {results}")

        return results



    def adopt_from_physical_positions(self, limit_per_symbol: int = 500) -> dict:
        from engine.write_queue import WriteQueue
        return WriteQueue().put_and_wait(self._adopt_from_physical_positions_internal, limit_per_symbol, _wq_timeout=120.0)

    def _adopt_from_physical_positions_internal(self, limit_per_symbol: int = 500) -> dict:

        """

        🔬 BIDIRECTIONAL PROOF RECONCILIATION — Cross-Reference Physical ↔ Ledger.

        """

        logger.info("🔬 [PHYS-ADOPT] Starting bidirectional proof reconciliation scan...")

        results = {}

        ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)

        if not ex:

            logger.error("[PHYS-ADOPT] No exchange interface available.")

            return {}



        conn = get_connection()

        cursor = conn.cursor()

        try:

            phys_positions = {}

            try:

                raw_positions = ex.fetch_positions()

                if raw_positions is None: return {}

            except Exception as _pe:

                logger.error(f"[PHYS-ADOPT] Failed to fetch positions: {_pe}")

                return {}



            for p in raw_positions:

                pos_qty = float(p.get('contracts') or 0)

                if abs(pos_qty) < 1e-9: continue

                symbol = normalize_symbol(p.get('symbol', ''))

                side = str(p.get('side', '')).upper()

                if not symbol or side not in ('LONG', 'SHORT'): continue

                phys_positions[(symbol, side)] = {'qty': abs(pos_qty), 'entry_price': float(p.get('entryPrice') or 0)}



            all_bots = conn.execute("SELECT b.id, b.pair, b.direction, b.name, t.current_step, t.basket_start_time, t.total_invested, t.entry_confirmed, COALESCE(t.cycle_id, 1), t.position_side, t.cycle_start_time, COALESCE(t.open_qty, 0.0) FROM bots b LEFT JOIN trades t ON t.bot_id=b.id WHERE b.is_active=1").fetchall()

            from collections import defaultdict

            # Aggregation step: map symbol -> list of bots sharing that ticker

            sym_group = defaultdict(list)

            for row in all_bots:

                bid, pair, direction, name, cur_step, bst, cur_inv, entry_conf, cycle_id, t_side, cst, open_qty = row

                sym = normalize_symbol(pair)

                d = str(t_side or direction or 'LONG').upper()

                sym_group[sym].append({

                    'bot_id': bid, 'name': name or '', 'pair': pair, 

                    'direction': d, 'current_step': int(cur_step or 0), 

                    'basket_start_time': int(bst or 0), 'total_invested': float(cur_inv or 0), 

                    'entry_confirmed': int(entry_conf or 0), 'cycle_id': int(cycle_id or 1),

                    'cycle_start_time': int(cst or bst or 0),

                    'open_qty': float(open_qty or 0.0)

                })



            # Fetch net physical positions (Normalised in Pass 1 to LONG/SHORT based on sign)

            phys_by_sym = {}

            for p in raw_positions:

                sym = normalize_symbol(p.get('symbol', ''))

                size = float(p.get('contracts') or 0)

                if abs(size) < 1e-9: continue

                # In One-Way mode, the signed size is the Absolute Truth.

                phys_by_sym[sym] = phys_by_sym.get(sym, 0.0) + size



            # Loop through symbols where we have bots or physical presence

            for symbol in set(sym_group.keys()) | set(phys_by_sym.keys()):

                net_phys_qty = phys_by_sym.get(symbol, 0.0)

                bots_on_ticker = sym_group.get(symbol, [])

                if not bots_on_ticker: continue



                # ── [v3.1.4] IDEMPOTENCY GUARD ─────────────────────────────────────

                # If ALL bots on this ticker are already REQUIRE_MANUAL_PROOF, skip.

                # The forensic scan already ran and failed. Re-running every reconciler

                # cycle is expensive and changes nothing. Human must intervene first.

                all_proof_required = all(

                    cursor.execute("SELECT status FROM bots WHERE id=?", (b['bot_id'],)).fetchone()[0] == 'REQUIRE_MANUAL_PROOF'

                    for b in bots_on_ticker

                )

                if all_proof_required:

                    logger.debug(f"[ADOPT-SKIP] {symbol}: All bots already REQUIRE_MANUAL_PROOF. Skipping scan.")

                    continue

                # ────────────────────────────────────────────────────────────────────



                # ── [v3.1.4] WIPE-WALL GATE ────────────────────────────────────────

                # If physical position predates the earliest wipe_wall_ts on this ticker,

                # it's a pre-session orphan. The system intentionally doesn't track it.

                # Don't try to reconcile pre-session positions against current-cycle accounting.

                if abs(net_phys_qty) > 1e-8:

                    min_wipe_wall = min(

                        (int(cursor.execute("SELECT wipe_wall_ts FROM trades WHERE bot_id=?", (b['bot_id'],)).fetchone()[0] or 0))

                        for b in bots_on_ticker

                    )

                    if min_wipe_wall > 0:

                        # Check if the physical position has any fills after the wipe wall

                        try:

                            recent_fills = ex.fetch_my_trades(symbol, limit=50) or []

                            # 🛡️ ARCHITECT'S GUARD v3.1.2: Soften Wipe-Wall Gate

                            # We block reconciliation ONLY if the position is a pure "Pre-session Orphan".

                            # If there are ANY fills (CQB or anonymous) post-wipe-wall, we allow

                            # the pass to continue, enabling forensic scans to resolve the gap.

                            post_wall_activity = any(

                                int((f.get('timestamp') or 0) // 1000) >= min_wipe_wall

                                for f in recent_fills

                            )

                            if not post_wall_activity:

                                logger.warning(

                                    f"[WIPE-WALL-GATE] {symbol}: Physical position {net_phys_qty:.4f} "

                                    f"is a pure Pre-session Orphan (all fills pre-date {min_wipe_wall}). "

                                    f"Skipping reconciliation. Monitor will show mismatch."

                                )

                                continue

                        except Exception as _wg_err:

                            logger.debug(f"[WIPE-WALL-GATE] {symbol}: Fill check failed (non-blocking): {_wg_err}")

                # ────────────────────────────────────────────────────────────────────



                # 🚀 NET-SUM PROOF CALCULATION:

                # We must verify if (Net_Ledger_Sum) == (Net_Physical_Total)

                total_net_proved_qty = 0.0

                history_restricted = False

                raw_fills = []

                

                # Scanning fills for ALL bots on this ticker simultaneously

                try:

                    raw_fills = ex.fetch_my_trades(symbol, limit=max(limit_per_symbol, 1000)) or []

                    if abs(net_phys_qty) > 0 and not any(str(f.get('clientOrderId') or '').startswith("CQB_") for f in raw_fills):

                        logger.info(f"🔍 [DEEP-SCAN] No CQB fills found for {symbol} in recent history. Scanning back 30 days...")

                        since_ts = int((time.time() - (30 * 24 * 60 * 60)) * 1000)

                        deep_fills = ex.fetch_my_trades(symbol, since=since_ts, limit=1000) or []

                        seen_ids = {str(f.get('id') or '') for f in raw_fills if f.get('id')}

                        for f in deep_fills:

                            fid = str(f.get('id') or '')

                            if fid and fid not in seen_ids:

                                raw_fills.append(f)

                                seen_ids.add(fid)

                except Exception as _e:

                    logger.warning(f"⚠️ [PHYS-ADOPT] History fetch restricted for {symbol}: {_e}")

                    history_restricted = True



                # Grouping fills for both directions

                grouped_fills = {}

                for fill in raw_fills:

                    oid = str(fill.get('order') or fill.get('orderId') or fill.get('id') or '')

                    if not oid: continue

                    cid = str(fill.get('clientOrderId') or '')
                    if not cid:
                        db_row = cursor.execute("SELECT client_order_id FROM bot_orders WHERE order_id = ?", (oid,)).fetchone()
                        if db_row and db_row[0]:
                            cid = db_row[0]

                    # In One-Way mode, positionSide is 'BOTH', so we don't filter by side.

                    # We adopt all CQB fills for a ticker.

                    

                    if oid not in grouped_fills:

                        grouped_fills[oid] = {'cid': cid, 'side': fill.get('side', ''), 'qty': 0.0, 'cost': 0.0, 'ts': int((fill.get('timestamp') or 0) // 1000)}

                    f_qty = float(fill.get('amount') or fill.get('filled') or 0)

                    f_price = float(fill.get('price') or 0)

                    if f_qty > 0 and f_price > 0:

                        grouped_fills[oid]['qty'] += f_qty

                        grouped_fills[oid]['cost'] += f_qty * f_price



                cursor = conn.cursor()

                for bot_info in bots_on_ticker:

                    bot_id = bot_info['bot_id']

                    bot_dir = bot_info['direction']

                    dna_prefix = f"CQB_{bot_id}_"

                    

                    # Adoption loop remains bot-specific, but uses the ticker-wide grouped_fills

                    sys_orders = cursor.execute("SELECT id, order_id, client_order_id, order_type, price, amount, filled_amount, status, step FROM bot_orders WHERE bot_id=? AND status NOT IN ('cancelled','canceled','reset_cleared', 'failed', 'rejected', 'auto_closed', 'audit', 'pending_placement') AND client_order_id LIKE ?", (bot_id, dna_prefix + "%")).fetchall()

                    for row in sys_orders:

                        if row[7] not in ('filled', 'closed'):

                            try:

                                if row[1] and any(str(row[1]).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_', 'VN_')):
                                    logger.info(f"🔎 [ADOPT-SCAN] Skipping fetch_order for synthetic order ID {row[1]}")
                                    ex_order = None
                                else:
                                    ex_order = ex.fetch_order(row[1], symbol)

                                ex_f = float(ex_order.get('filled') or 0)
                                if ex_f > 0:
                                    from engine.ledger import credit_fill
                                    credit_fill(
                                        bot_id=bot_id,
                                        order_id=str(row[1] or row[2]),
                                        cumulative_qty=ex_f,
                                        avg_price=float(
                                            ex_order.get('average')
                                            or ex_order.get('price')
                                            or 0
                                        ),
                                        order_type=str(row[3] or 'grid').lower(),
                                        is_cumulative=True,
                                    )

                            except: continue



                    _wall_ts = cursor.execute("SELECT wipe_wall_ts FROM trades WHERE bot_id=?", (bot_id,)).fetchone()

                    wipe_wall = int(_wall_ts[0] or 0) if _wall_ts else 0



                    existing_oids = {r[0] for r in cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id=?", (bot_id,)).fetchall() if r[0]}

                    bot_specific_fills = [
                        (fill_oid, g_data) for fill_oid, g_data in grouped_fills.items()
                        if g_data['cid'].startswith(dna_prefix)
                    ]
                    bot_specific_fills.sort(key=lambda x: x[1]['ts'], reverse=True)

                    allowed_fill_oids = set()
                    if abs(net_phys_qty) > 1e-8:
                        expected_bot_dir = 'LONG' if net_phys_qty > 0 else 'SHORT'
                        if bot_dir == expected_bot_dir:
                            other_open_qty = sum(
                                float(other_bot.get('open_qty', 0.0))
                                for other_bot in bots_on_ticker
                                if other_bot['bot_id'] != bot_id and other_bot['direction'] == bot_dir
                            )
                            target_qty = max(0.0, abs(net_phys_qty) - other_open_qty)
                            running_qty = float(bot_info.get('open_qty', 0.0))
                            phys_side = 'buy' if net_phys_qty > 0 else 'sell'
                            for fill_oid, g_data in bot_specific_fills:
                                if running_qty >= target_qty - 1e-6:
                                    break
                                if g_data['side'].lower() == phys_side:
                                    running_qty += g_data['qty']
                                    allowed_fill_oids.add(fill_oid)

                    # [v3.5.4] Circuit Breaker: Track how much qty we would adopt/align for this bot
                    would_adopt_qty = 0.0
                    for fill_oid, g_data in bot_specific_fills:
                        if fill_oid not in allowed_fill_oids:
                            continue
                        existing_row = cursor.execute("SELECT cycle_id FROM bot_orders WHERE order_id = ?", (fill_oid,)).fetchone()
                        if existing_row:
                            db_cycle = existing_row[0]
                            if db_cycle != bot_info['cycle_id']:
                                would_adopt_qty += g_data['qty']
                        else:
                            if fill_oid not in existing_oids:
                                would_adopt_qty += g_data['qty']

                    max_allowed_adopt = getattr(config, 'MAX_ADOPTION_QTY_PER_CYCLE', 0.5)
                    if would_adopt_qty > max_allowed_adopt:
                        logger.error(f"🚨 [ADOPT-LIMIT-EXCEEDED] Bot {bot_id} on {symbol}: Attempting to adopt/align {would_adopt_qty:.6f} units, which exceeds MAX_ADOPTION_QTY_PER_CYCLE limit of {max_allowed_adopt:.6f}. Aborting adoption and freezing.")
                        cursor.execute("UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=?", (bot_id,))
                        conn.commit()
                        continue

                    for fill_oid, g_data in bot_specific_fills:
                        if fill_oid not in allowed_fill_oids:
                            continue

                        # Check if the order already exists in our database
                        existing_row = cursor.execute("SELECT id, cycle_id, status FROM bot_orders WHERE order_id = ?", (fill_oid,)).fetchone()
                        if existing_row:
                            db_row_id, db_cycle, db_status = existing_row
                            if db_cycle != bot_info['cycle_id']:
                                fill_ts = int(g_data.get('ts') or 0)
                                if wipe_wall > 0 and fill_ts < wipe_wall:
                                    logger.warning(
                                        f"[ADOPT-WALL-GUARD] Skipping cycle realignment for order {fill_oid} "
                                        f"on bot {bot_id}: fill_ts={fill_ts} < wipe_wall={wipe_wall}. "
                                        f"This fill belongs to a previous cycle."
                                    )
                                    continue
                                cursor.execute("UPDATE bot_orders SET cycle_id = ?, status = 'filled' WHERE id = ?", (bot_info['cycle_id'], db_row_id))
                                logger.info(f"🔄 [ADOPT-CYCLE-ALIGN] Moved order {fill_oid} from cycle {db_cycle} (status={db_status}) to active cycle {bot_info['cycle_id']} with status='filled' to align with open physical position.")
                            continue

                        if fill_oid not in existing_oids:
                            fill_ts = int(g_data.get('ts') or 0)
                            if wipe_wall > 0 and fill_ts < wipe_wall:
                                logger.warning(
                                    f"[ADOPT-WALL-GUARD] Skipping adoption of order {fill_oid} "
                                    f"on bot {bot_id}: fill_ts={fill_ts} < wipe_wall={wipe_wall}. "
                                    f"Pre-cycle fill cannot be adopted into current cycle."
                                )
                                continue
                            # 🚀 HEDGE-MODE FIX: ensure adoption rows carry the correct side for the bot
                            cursor.execute("INSERT OR IGNORE INTO bot_orders (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, step, cycle_id, created_at, position_side) VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', 0, ?, ?, ?)", (bot_id, fill_oid, g_data['cid'], g_data['cost']/g_data['qty'], g_data['qty'], g_data['qty'], bot_info['cycle_id'], g_data['ts'], bot_dir))

                    conn.commit()
                    from engine.database import recompute_invested_from_orders, sync_trades_from_orders
                    _, _, true_qty, _ = recompute_invested_from_orders(bot_id)
                    # Signed contribution to net sum.
                    # 🛡️ ARCHITECT'S GUARD: true_qty from recompute_invested_from_orders already
                    # accounts for entry/grid vs tp/close vs hedge. No manual subtraction needed.
                    total_net_proved_qty += (true_qty if bot_dir == 'LONG' else -true_qty)

                    # 🔬 HISTORICAL NET: cross-cycle cumulative sum for PASS 3 Tier-2 check.
                    # DNA-GUARD v3.0: If current-cycle mismatches, historical net explains whether
                    # prior-cycle accumulation accounts for the difference.

                    hist_opened = cursor.execute("""
                        SELECT COALESCE(SUM(filled_amount), 0.0)
                        FROM bot_orders
                        WHERE bot_id=? AND filled_amount > 0
                        AND order_type IN ('entry','grid','adoption','adoption_add')
                    """, (bot_id,)).fetchone()[0] or 0.0



                    # 'hedge' order_type intentionally excluded — no hedge orders exist post ADR-002
                    hist_closed = cursor.execute("""
                        SELECT COALESCE(SUM(filled_amount), 0.0)
                        FROM bot_orders
                        WHERE bot_id=? AND filled_amount > 0
                        AND order_type IN ('tp','close','exit','adoption_reduce','dust_close','sl','flatten_close')
                    """, (bot_id,)).fetchone()[0] or 0.0



                    hist_net = float(hist_opened) - float(hist_closed)

                    bot_info['_hist_net'] = hist_net  # Stash for PASS 3



                    # Promotion Gate: Individually promote bots if they match their own ledger

                    if true_qty > 0:

                        cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=? AND status IN ('Scanning', '🟢 SCANNING', 'REQUIRE_MANUAL_PROOF')", (bot_id,))

                        sync_trades_from_orders(bot_id)





                # 🚀 PASS 3: GLOBAL NET VERIFICATION (TWO-TIER)

                # Tier 1: Current-cycle net must match physical (primary check)

                # Tier 2: If current-cycle mismatches, check HISTORICAL net (all cycles).

                #         Physical positions are cumulative — they include prior cycles' fills

                #         that were marked reset_cleared after cycle turnovers.

                

                # Fetch a dynamic reference price to scale the USD threshold properly.

                # [v3.3.1 COLUMN-FIX]: Previous query used 'cached_avg_price' which is not

                # a real column in the trades schema (correct name: avg_entry_price).

                # The silent exception caused ref_price to always fall back to 1.0, making

                # diff_usd = diff_qty * 1.0 regardless of asset price. For BTC at $80k,

                # a 0.001 BTC gap ($80) was evaluated against a $0.05 threshold — always

                # triggered REQUIRE_MANUAL_PROOF. Now uses avg_entry_price correctly.

                ref_price = 1.0

                try:

                    _p_row = conn.execute(

                        "SELECT AVG(t.avg_entry_price) FROM trades t "

                        "JOIN bots b ON b.id=t.bot_id "

                        "WHERE (b.pair=? OR b.normalized_pair=?) AND t.avg_entry_price > 0",

                        (symbol, symbol)

                    ).fetchone()

                    if _p_row and _p_row[0]: ref_price = float(_p_row[0])

                except Exception as _e:
                    logger.debug(f'[REF-PRICE] Failed to fetch avg_entry_price for threshold scaling: {_e}')



                diff_qty = abs(total_net_proved_qty - net_phys_qty)

                diff_usd = diff_qty * ref_price



                # Dynamic USD tolerance: 2% or $0.05 minimum (aligns with UI monitor mismatch triggers)

                if diff_usd > max(abs(net_phys_qty * ref_price) * 0.02, 0.05):

                    # Guard C: If the pair physical position is flat, but we are deferring global flatten
                    # due to freshness (Guard B) or waiting for 3 consecutive flat snapshots (Guard A),
                    # do NOT escalate to REQUIRE_MANUAL_PROOF.
                    try:
                        if abs(net_phys_qty) < 0.0001:
                            db_conn = get_connection()
                            max_checked_row = db_conn.execute("SELECT MAX(last_checked) FROM active_positions").fetchone()
                            max_checked = max_checked_row[0] if max_checked_row else None
                            current_ts = int(time.time())
                            is_stale = not max_checked or (current_ts - max_checked) > 60

                            flat_count = getattr(self, '_flat_snapshots_counts', {}).get(symbol, 0)

                            if is_stale or flat_count < 3:
                                logger.info(
                                    f"⏳ [PASS3-DEFER] Ticker {symbol}: Physical is flat, but deferring "
                                    f"REQUIRE_MANUAL_PROOF due to global-flatten guards (stale={is_stale}, count={flat_count}/3)."
                                )
                                continue  # Skip to next ticker, do NOT gate as REQUIRE_MANUAL_PROOF
                    except Exception as _defer_err:
                        logger.debug(f"[PASS3-DEFER] Defer check failed: {_defer_err}")



                    # ── RECENT-FILL GRACE PERIOD [v2.5] ──────────────────────────────────

                    # After a WS fill event fires, the DB commit from seal_trade_state runs

                    # asynchronously. There is a 1–5s window where the physical position

                    # already exists on the exchange but the ledger has not yet been sealed.

                    # If PASS 3 runs inside this window, the mismatch is transient — NOT

                    # structural. Triggering REQUIRE_MANUAL_PROOF here would be a false alarm.

                    #

                    # Guard: if ANY bot on this ticker had a bot_orders row created or updated

                    # within the last 90 seconds (filled_at or updated_at), skip escalation.

                    # The next reconciler cycle (30–60s away) will see a clean ledger.

                    try:

                        _bot_ids_on_ticker = [b['bot_id'] for b in bots_on_ticker]

                        _placeholders = ','.join('?' * len(_bot_ids_on_ticker))

                        _recent_fill_cutoff = int(time.time()) - 90  # 90s grace window

                        _recent_fill = cursor.execute(

                            f"SELECT COUNT(*) FROM bot_orders "

                            f"WHERE bot_id IN ({_placeholders}) "

                            f"AND (filled_at >= ? OR updated_at >= ?) "

                            f"AND filled_amount > 0 "

                            f"AND order_type IN ('entry','grid','adoption','adoption_add','tp','close')",

                            (*_bot_ids_on_ticker, _recent_fill_cutoff, _recent_fill_cutoff)

                        ).fetchone()[0]

                        # FIX #3 [V2.4.1]: Add a size threshold to PASS3-GRACE.

                        # After a 3+ day offline window, gaps of 80-180+ units can appear because

                        # offline TP fills are not in bot_orders. Gracing these large gaps causes

                        # the bot to run with the wrong open_qty for minutes, triggering cascading

                        # mismatches. Only grace-skip if the gap is small (likely a DB seal lag).

                        _GRACE_MAX_UNITS = 20.0  # Units threshold — gaps > 20 always get forensic scan

                        _gap_abs = abs(total_net_proved_qty - net_phys_qty)

                        if _recent_fill > 0 and _gap_abs <= _GRACE_MAX_UNITS:

                            logger.info(

                                f"⏳ [PASS3-GRACE] Ticker {symbol}: "

                                f"Net mismatch detected (Proved={total_net_proved_qty:.4f} vs Phys={net_phys_qty:.4f}) "

                                f"but {_recent_fill} recent fill(s) within 90s window and gap={_gap_abs:.2f} ≤ {_GRACE_MAX_UNITS} units. "

                                f"Skipping forensic scan / REQUIRE_MANUAL_PROOF — likely seal_trade_state lag."

                            )

                            continue  # Skip to next ticker

                        elif _recent_fill > 0 and _gap_abs > _GRACE_MAX_UNITS:

                            logger.warning(

                                f"⚠️ [PASS3-GRACE-OVERRIDE] Ticker {symbol}: "

                                f"Gap={_gap_abs:.2f} units EXCEEDS grace threshold ({_GRACE_MAX_UNITS}). "

                                f"Forcing forensic scan despite {_recent_fill} recent fill(s). "

                                f"Large gaps may indicate offline fill loss — exchange truth wins."

                            )

                    except Exception as _grace_err:

                        logger.debug(f"[PASS3-GRACE] Grace period check failed (non-blocking): {_grace_err}")

                    # ─────────────────────────────────────────────────────────────────────



                    # Compute cross-cycle historical net sum

                    hist_total_net = sum(

                        (b['_hist_net'] if b.get('direction') == 'LONG' else -b.get('_hist_net', 0.0))

                        for b in bots_on_ticker if '_hist_net' in b

                    )

                    if abs(hist_total_net - net_phys_qty) <= max(abs(net_phys_qty)*0.02, 0.01):

                        # ✅ HISTORICAL NET MATCHES — The gap is fully explained by prior-cycle fills.

                        # The current-cycle ledger is CORRECT; it just doesn't include closed/reset cycles.

                        # No REQUIRE_MANUAL_PROOF needed — bots are legitimately accumulating.

                        logger.info(

                            f"✅ [PASS3-HIST] Ticker {symbol}: Current-cycle mismatch "

                            f"(Proved={total_net_proved_qty:.4f}, Phys={net_phys_qty:.4f}) "

                            f"is explained by historical cross-cycle net ({hist_total_net:.4f}). "

                            f"Skipping REQUIRE_MANUAL_PROOF — position accumulation is valid."

                        )

                    else:
                        # ─── 🛡️ INVARIANT §3.57 + §3.58: PROOF GRACE — RECENT FILL WINDOW ───────
                        # Uses the shared pair_has_recent_fill helper (engine.parity_gates).
                        # bot_ids=_bot_ids_grace2 preserves the exact bots_on_ticker semantics.
                        # window_seconds=60: any fill type; tp_close_window_seconds=600: TP/close.
                        # max_gap_units=20.0: mirrors PASS3-GRACE FIX #3 [V2.4.1] — gaps > 20
                        # units are forced through to the forensic scan even if a recent fill
                        # exists (offline-fill losses produce large gaps, not DB-seal lag).
                        # updated_at is used (set on fill credit, not order creation).
                        # ────────────────────────────────────────────────────────────────────────
                        try:
                            from engine.parity_gates import pair_has_recent_fill as _phrf
                            _bot_ids_grace2 = [b['bot_id'] for b in bots_on_ticker]
                            _gap_abs = abs(total_net_proved_qty - net_phys_qty)
                            if _phrf(
                                cursor,
                                window_seconds=60,
                                tp_close_window_seconds=600,
                                bot_ids=_bot_ids_grace2,
                                max_gap_units=20.0,
                                gap_abs=_gap_abs,
                            ):
                                logger.info(
                                    f"⏳ [PROOF-GRACE] {symbol}: mismatch of "
                                    f"{_gap_abs:.6f} units \u2264 20 units cap "
                                    f"with recent fill within grace window — "
                                    f"deferring proof gate by 1 reconciler cycle. "
                                    f"(Proved={total_net_proved_qty:.6f}, Phys={net_phys_qty:.6f})"
                                )
                                continue  # Skip to next ticker — do NOT set REQUIRE_MANUAL_PROOF

                        except Exception as _grace2_err:
                            logger.debug(f"[PROOF-GRACE] Secondary recency check failed (non-blocking): {_grace2_err}")





                        # ─────────────────────────────────────────────────────────────────────

                        # ─── 🩹 AUTONOMOUS SELF-HEAL (before giving up) ──────────────────────

                        # Both current-cycle AND historical nets don't match physical.

                        # Attempt a targeted 48h forensic scan to recover truly missing fills.

                        logger.warning(


                            f"⚠️ [PROOF-FAILED] Ticker {symbol} NET Mismatch: "

                            f"Physical {net_phys_qty:.6f} vs Current-Proved {total_net_proved_qty:.6f} "

                            f"vs Historical {hist_total_net:.6f}. "

                            f"Attempting autonomous forensic fill scan before setting REQUIRE_MANUAL_PROOF..."

                        )

                        try:

                            # BYPASS all cooldowns for a forced targeted scan on this pair

                            pair_key = f'_last_pair_scan_{symbol}'

                            if hasattr(StateReconciler, pair_key):

                                delattr(StateReconciler, pair_key)



                            logger.info(f"🔬 [AUTONOMOUS-HEAL] Running 48h forensic fill scan for {symbol}...")

                            self.reconstruct_offline_fills(since_hours=48, pair_filter=symbol)



                            # Re-compute the proved qty after the forensic fill insertion

                            total_net_proved_qty_v2 = 0.0

                            for bot_info_v2 in bots_on_ticker:

                                from engine.database import recompute_invested_from_orders as _rif

                                _, _, heal_qty, _ = _rif(bot_info_v2['bot_id'])

                                total_net_proved_qty_v2 += (heal_qty if bot_info_v2['direction'] == 'LONG' else -heal_qty)



                            if abs(total_net_proved_qty_v2 - net_phys_qty) <= max(abs(net_phys_qty)*0.02, 0.01):

                                # Autonomous heal succeeded — promote all bots that now have proved qty

                                logger.info(

                                    f"✅ [AUTONOMOUS-HEAL] Ticker {symbol}: Forensic scan closed the gap! "

                                    f"Physical {net_phys_qty:.6f} ≈ Proved {total_net_proved_qty_v2:.6f}. "

                                    f"Promoting bots to IN TRADE."

                                )

                                for b_info in bots_on_ticker:

                                    _, _, b_qty, _ = _rif(b_info['bot_id'])

                                    if b_qty > 0:

                                        cursor.execute(

                                            "UPDATE bots SET status='IN TRADE' WHERE id=? AND status IN ('Scanning','🟢 SCANNING','REQUIRE_MANUAL_PROOF')",

                                            (b_info['bot_id'],)

                                        )

                                        from engine.database import sync_trades_from_orders

                                        sync_trades_from_orders(b_info['bot_id'])

                                conn.commit()

                            else:

                                # ════════════════════════════════════════════════════════════

                                # [v3.1.4] BIDIRECTIONAL FORENSIC ADOPTION DISABLED

                                # ════════════════════════════════════════════════════════════

                                # REASON: The adoption was self-reinforcing and catastrophically

                                # broken. Each forensic_adoption_add record (CQB_ADOPT_ ID) gets

                                # counted as a POSITIVE FILL by get_pair_virtual_net() on the

                                # next reconciler cycle. This makes the apparent mismatch DOUBLE,

                                # triggering another adoption twice as large — ad infinitum.

                                # Observed: 1063 → 2126 → 4253 → ... → 8,711,372 → 17M XRP

                                #

                                # CORRECT BEHAVIOR:

                                #   (A) If the residual gap is sub-threshold (dust-level), write a

                                #       'drift_note' audit record (filled_amount=0 — permanently

                                #       invisible to get_pair_virtual_net()) and treat it as resolved.

                                #   (B) If the gap is material, escalate to REQUIRE_MANUAL_PROOF.

                                #

                                # ⛔ CRITICAL — 'drift_note' MUST stay out of BOTH lists in §3.9:

                                #   Entries: 'entry','grid','adoption','adoption_add','carry'

                                #   Exits:   'tp','close','sl','dust_close','adoption_reduce','virtual_netting'

                                # Any type from those lists counts toward ledger math on next cycle.

                                # 'drift_note' falls through to ELSE 0 in the SQL — zero ledger impact.

                                # ════════════════════════════════════════════════════════════



                                residual_qty = abs(total_net_proved_qty_v2 - net_phys_qty)

                                residual_usd = residual_qty * ref_price



                                # ── [v3.1.4] FRACTIONAL DRIFT SWEEPER ──────────────────────

                                # Thresholds: gap must be BOTH < $5 USD AND < 0.5 units.

                                # These are deliberately conservative — any larger gap must have

                                # human eyes on it.  The note record carries full diagnostics so

                                # a future audit can reconstruct exactly what was swept.

                                _DRIFT_MAX_USD   = 5.0    # $5 maximum auto-sweep

                                _DRIFT_MAX_UNITS = 0.5    # 0.5 units maximum auto-sweep



                                if residual_usd <= _DRIFT_MAX_USD and residual_qty <= _DRIFT_MAX_UNITS:

                                    logger.info(

                                        f"🧹 [DRIFT-SWEEP] Ticker {symbol}: Residual gap "

                                        f"({residual_qty:.6f} units / ${residual_usd:.4f}) is within "

                                        f"dust threshold (≤{_DRIFT_MAX_UNITS}u / ≤${_DRIFT_MAX_USD}). "

                                        f"Writing drift_note audit record (filled_amount=0, "

                                        f"invisible to ledger math) and skipping REQUIRE_MANUAL_PROOF."

                                    )

                                    # Write ONE audit row per affected bot.

                                    # filled_amount=0 → passes the `AND o.filled_amount > 0`

                                    # guard in get_pair_virtual_net() → contributes exactly 0.

                                    _drift_ts = int(time.time())

                                    for b_info in bots_on_ticker:

                                        sym_short = symbol.split(':')[0].replace('/', '')[:7]

                                        _note_cid = (

                                            f"CQB_{b_info['bot_id']}_DF_"

                                            f"{sym_short}_{_drift_ts}"

                                        )

                                        try:

                                            cursor.execute(

                                                """INSERT OR IGNORE INTO bot_orders

                                                   (bot_id, order_id, client_order_id,

                                                    order_type, price, amount, filled_amount,

                                                    status, step, cycle_id, created_at,

                                                    position_side, notes)

                                                   VALUES (?,?,?,'drift_note',?,?,0,'audit',

                                                           0,?,?,?,?)""",

                                                (

                                                    b_info['bot_id'],

                                                    f"DRIFT_{b_info['bot_id']}_{_drift_ts}",

                                                    _note_cid,

                                                    ref_price,

                                                    residual_qty,

                                                    b_info.get('cycle_id'),

                                                    _drift_ts,

                                                    b_info.get('direction', 'LONG'),

                                                    (

                                                        f"AutoSweep: proved={total_net_proved_qty_v2:.6f} "

                                                        f"phys={net_phys_qty:.6f} "

                                                        f"gap={residual_qty:.6f}u "

                                                        f"(${residual_usd:.4f})"

                                                    )

                                                )

                                            )

                                        except Exception as _dn_err:

                                            logger.debug(f"[DRIFT-SWEEP] drift_note insert warn: {_dn_err}")

                                    conn.commit()



                                else:

                                    # Gap is material — human review required.

                                    logger.error(

                                        f"🚨 [PROOF-FAILED] Ticker {symbol}: Persistent mismatch "

                                        f"cannot be resolved by forensic scan. "

                                        f"System={total_net_proved_qty_v2:.6f} vs Physical={net_phys_qty:.6f}. "

                                        f"Diff={residual_qty:.6f} units / ${residual_usd:.2f}. "

                                        f"Setting REQUIRE_MANUAL_PROOF — check Monitor → Global Netting → {symbol}."

                                    )

                                    for b_info in bots_on_ticker:

                                        cursor.execute(

                                            "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=? "

                                            "AND status NOT IN ('Scanning', 'stopped')",

                                            (b_info['bot_id'],)

                                        )

                                    conn.commit()

                        except Exception as _heal_err:

                            logger.error(f"[AUTONOMOUS-HEAL] Forensic scan failed for {symbol}: {_heal_err}")

                            # 🚀 ROOT CAUSE FIX: Isolate REQUIRE_MANUAL_PROOF to specific bots

                            for b_info in bots_on_ticker:

                                from engine.database import recompute_invested_from_orders as _rif

                                _, _, b_qty, _ = _rif(b_info['bot_id'])

                                if b_qty <= 0 and b_info.get('total_invested', 0) > 0:

                                    logger.warning(f"🚨 [PROOF-FAILED] Bot {b_info['bot_id']} ({b_info['name']}) has proven gap. Freezing.")

                                    cursor.execute("UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=?", (b_info['bot_id'],))

                            conn.commit()

                            logger.critical(

                                f"🚨 [PROOF-FAILED] Ticker {symbol} NET Mismatch: "

                                f"Physical {net_phys_qty:.6f} vs Proved-Net {total_net_proved_qty:.6f}. "

                                f"Problematic bots moved to REQUIRE_MANUAL_PROOF."

                            )

                        # ─────────────────────────────────────────────────────────────────────





        except Exception as e:

            logger.error(f"[PHYS-ADOPT] Fatal: {e}", exc_info=True)

        return results



DeepReconciler = StateReconciler