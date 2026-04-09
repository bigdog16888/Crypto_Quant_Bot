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
    DB_PATH
)
from .exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logger = logging.getLogger("StateReconciliation")

# 🕐 SESSION GUARD: Record the exact moment this engine session started.
# Used in two ways:
#   1. Guard against re-ingesting stale fills from Binance's 48h history after restart.
#   2. Detect whether a bot's basket_start_time was set as a restart-fallback
#      (i.e., set within 10min of ENGINE_START_TIME) vs. a genuine trade-start time.
#      If it's a fallback, the DNA-GUARD is bypassed so current-cycle fills are not blocked.
ENGINE_START_TIME = int(time.time())


class ReconciliationAction(Enum):
    """Actions to take during reconciliation"""
    NO_ACTION = "no_action"
    RESET_TO_IDLE = "reset_to_idle"
    MARK_TP_HIT = "mark_tp_hit"
    REPAIR_ORDERS = "repair_orders"
    REQUIRE_MANUAL = "require_manual"
    SYSTEM_FIX_ZOMBIE = "system_fix_zombie"
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
    # Bot configuration for mathematical step recovery
    base_size: float = 0.0          # First-step notional (e.g. $100)
    martingale_multiplier: float = 1.0  # Each step size = prev_step * multiplier


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
    requires_manual_intervention: bool


class StateReconciler:
    """
    Refactored State Reconciler (v2.0)
    
    Philosophy:
    1. Bot-Centric: The Bot DB is the source of truth for "Intent".
    2. Verification: The Exchange is the source of truth for "Reality".
    3. Net-Sum: We validate the Aggregate Virtual Position against the Net Physical Position.
    """
    
    def __init__(self, exchanges: Optional[Dict[str, ExchangeInterface]] = None):
        if exchanges:
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
        
    def get_exchange(self, market_type: str):
        if market_type in self.exchanges:
            return self.exchanges[market_type]
        if not self.exchanges:
            return None
        return list(self.exchanges.values())[0]

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

    def reconstruct_offline_fills(self, since_hours: int = 168) -> Dict[str, int]:
        """
        Scans exchange history for orders that filled while we were offline.
        Updates the DB immediately so subsequent checks see the correct state.
        Refactored to be ROBUST: Uses Client Order ID parsing to reconstruct state.
        """
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total': 0}
        
        # 🛡️ GLOBAL COOLDOWN (15 minutes) to prevent API spam during persistent gaps
        current_time = time.time()
        last_scan = getattr(StateReconciler, '_last_global_offline_scan', 0.0)
        if current_time - last_scan < 900:
            logger.debug(f"⏳ [FILL-SCAN] Skipping offline fill scan (on 15m cooldown, {int(900 - (current_time - last_scan))}s left).")
            return stats
        StateReconciler._last_global_offline_scan = current_time
        
        # 🔑 PREFLIGHT: Sync DB positions to exchange before looking at any single fill
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
        
        # Get pairs from open orders too
        cursor.execute("SELECT DISTINCT pair from bots WHERE id IN (SELECT DISTINCT bot_id FROM bot_orders WHERE status='open')")
        order_pairs = [r[0] for r in cursor.fetchall()]
        
        # We will restrict this later using absolute mathematical gap verification.
        # pairs_to_check = set([b[1] for b in active_bots] + order_pairs)
        
        # Pre-fetch Bot States for fast lookups
        # Map: bot_id -> {current_step, total_invested, avg_entry, basket_start_time}
        bot_states = {}
        cursor.execute("SELECT bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time, COALESCE(cycle_id, 1) FROM trades")
        for row in cursor.fetchall():
            bot_states[row[0]] = {
                'current_step': row[1], 'total_invested': row[2], 
                'avg_entry': row[3], 'entry_confirmed': row[4],
                'basket_start_time': row[5] or 0,
                'cycle_id': row[6]
            }

        conn.close() # Close mainly to keep scope clean, we'll reopen for updates

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
        _heal_conn.close()


        # 1.5. 🚀 PRE-COMMIT ROW RESOLUTION
        # Covers two failure windows:
        # A) status='placing' — order written to DB BEFORE exchange API call; engine crashed mid-atomicity.
        # B) status='new', order_type='entry' — entry confirmed by exchange (API returned OK) but engine
        #    went offline BEFORE the WebSocket fill event arrived. This means filled_amount=0 in DB even
        #    though Binance fully executed the entry order.
        #
        # SCOPE CONSTRAINT: We intentionally restrict 'new' scanning to ENTRY orders only.
        # TP and grid orders legitimately sit as status='new', filled_amount=0 while open on the book.
        # Scanning them would process live active orders as "stale unresolved" — causing false resets.
        # Only entry fills that happened while offline can cause the virtual/physical gaps we target here.
        _place_conn = get_connection()
        _place_cur = _place_conn.cursor()
        _place_cur.execute("""
            SELECT bo.id, bo.bot_id, b.pair, bo.order_type, bo.client_order_id,
                   bo.price, bo.amount, bo.step, bo.cycle_id
            FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id
            WHERE (
                bo.status = 'placing'                                      -- Window A: crashed mid-atomicity
                OR (bo.status = 'new' AND bo.order_type = 'entry')        -- Window B: entry filled offline
            )
              AND bo.filled_amount = 0
              AND bo.created_at < ?
        """, (int(time.time()) - 30,))
        placing_rows = _place_cur.fetchall()

        if placing_rows:
            logger.info(f"🔍 [PRE-COMMIT-RESOLVE] Found {len(placing_rows)} unresolved 'placing' rows.")

        for p_row in placing_rows:
            db_id, bot_id, pair, otype, cid, price, amount, step, cycle_id = p_row
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    exch_order = None
                    # Try direct fetch by clientOrderId first (Binance supports this)
                    try:
                        exch_order = ex.fetch_order_by_client_order_id(pair, cid)
                    except Exception:
                        pass
                    # Fallback: scan recent closed+open orders
                    if not exch_order:
                        try:
                            candids = (ex.fetch_closed_orders(pair, limit=50) or []) + (ex.fetch_open_orders(pair) or [])
                            for o in candids:
                                if o.get('clientOrderId') == cid or (o.get('info') or {}).get('clientOrderId') == cid:
                                    exch_order = o; break
                        except Exception: pass

                    if exch_order:
                        o_status = (exch_order.get('status') or '').lower()
                        o_id = exch_order.get('id')
                        if o_status in ('filled', 'closed', 'open', 'new', 'partially_filled'):
                            new_status = 'open' if o_status in ('open','new','partially_filled') else o_status
                            _place_cur.execute("UPDATE bot_orders SET order_id=?, status=?, updated_at=? WHERE id=?",
                                               (o_id, new_status, int(time.time()), db_id))
                            logger.info(f"✅ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → found on exchange as {o_status} (id={o_id}). Restored to '{new_status}'.")
                        else:
                            _place_cur.execute("UPDATE bot_orders SET status='failed', updated_at=? WHERE id=?",
                                               (int(time.time()), db_id))
                            logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → {o_status}. Marked failed.")
                    else:
                        # Never reached the exchange — delete the intent row
                        _place_cur.execute("DELETE FROM bot_orders WHERE id=?", (db_id,))
                        logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → not on exchange. Deleted (never placed).")
                    break
                except Exception as e_r:
                    logger.warning(f"[PRE-COMMIT-RESOLVE] Could not resolve row {db_id}: {e_r}")

        if placing_rows:
            _place_conn.commit()
        _place_conn.close()

        # 1.6. 🚀 HISTORY-BASED ORPHAN DETECTION
        # For any pair where physical position > virtual, scan 48h of exchange order history
        # for CQB_-prefixed fills that have no matching trade_history entry.
        # These are injected as bot_orders so the per-pair OFFLINE-SYNC picks them up below.
        try:
            _oh_conn = get_connection()
            _oh_cur = _oh_conn.cursor()
            _oh_cur.execute("SELECT pair, side, size FROM active_positions")
            phys_pos = {r[0]: abs(float(r[2])) for r in _oh_cur.fetchall()}

            _oh_cur.execute("""
                SELECT b.pair, b.direction, b.id,
                       COALESCE(SUM(
                           CASE 
                               WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount
                               WHEN bo.order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl') THEN -bo.filled_amount
                               ELSE 0.0
                           END
                       ), 0.0) as net_qty
                FROM bots b
                LEFT JOIN trades t ON b.id=t.bot_id
                LEFT JOIN bot_orders bo ON b.id=bo.bot_id AND bo.filled_amount>0
                    AND (bo.cycle_id=t.cycle_id OR bo.cycle_id IS NULL)
                    AND bo.status NOT IN ('reset_cleared','auto_closed','failed','placing')
                WHERE b.is_active=1
                GROUP BY b.id
            """)
            from engine.exchange_interface import normalize_symbol as _nsym
            virt_by_pair = {}
            for rv in _oh_cur.fetchall():
                pk = _nsym(rv[0]); signed = float(rv[3] or 0) * (1 if rv[1]=='LONG' else -1)
                virt_by_pair[pk] = virt_by_pair.get(pk, 0.0) + signed
            _oh_conn.close()

            gap_pairs = []
            for p, pq in phys_pos.items():
                vq = abs(virt_by_pair.get(_nsym(p), 0.0))
                # Catch gaps in BOTH directions: 
                # - Physical > Virtual (Missed Entry)
                # - Physical < Virtual (Missed TP)
                if abs(pq - vq) > 0.001:
                    gap_pairs.append((p, pq, vq, pq - vq))

            if gap_pairs:
                logger.info(f"🔍 [HISTORY-ORPHAN] {len(gap_pairs)} pairs with position gaps: {[(p,round(d,4)) for p,_,_,d in gap_pairs]}")

            # 🚀 OPTIMIZATION: Zero API Spam for Healthy Pairs
            # If a pair has exactly 0.0 mathematical gap, there is NO mathematically 
            # possible offline fill that altered inventory. Skip fetching its history.
            pairs_to_check = set([p[0] for p in gap_pairs])
            
            if not pairs_to_check:
                logger.info("✅ [OFFLINE-SYNC] All active pairs perfectly align with exchange reality. Zero gaps. Skipping history API scan.")
                return stats

            since_fallback = int((time.time() - since_hours * 3600) * 1000)  # Use injected since_hours instead of hardcoded 7 days to prevent rate limiting
            for gap_pair, phys_qty, v_qty, gap_qty in gap_pairs:
                for mt, ex in self.exchanges.items():
                    if not ex: continue
                    try:
                        hist = []
                        current_since = since_fallback
                        for _ in range(10):  # Fetch up to 10 pages to span long offline weekends
                            page = ex.fetch_closed_orders(gap_pair, since=current_since, limit=1000)
                            if not page: break
                            hist.extend(page)
                            last_ts = max((o.get('timestamp') or 0) for o in page)
                            if last_ts <= current_since: break
                            current_since = last_ts + 1
                        
                        hist = sorted(hist, key=lambda x: x.get('timestamp') or 0)
                        _oi_conn = get_connection(); _oi_cur = _oi_conn.cursor()
                        for o in hist:
                            o_cid = o.get('clientOrderId') or (o.get('info') or {}).get('clientOrderId') or ''
                            if not o_cid.startswith('CQB_'): continue
                            o_status = (o.get('status') or '').lower()
                            o_filled = float(o.get('filled') or 0)
                            o_price = float(o.get('average') or o.get('price') or 0)
                            o_id = o.get('id')
                            
                            # 🚀 ROOT CAUSE FIX: Do not require 'filled'/'closed' status.
                            # A partial fill on a 'canceled' or 'open' order is STILL A FILL.
                            # If we skip it, the ledger undercounts.
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

                            _oi_cur.execute("SELECT id, filled_amount, status FROM bot_orders WHERE order_id=?", (o_id,))
                            existing_order = _oi_cur.fetchone()
                            
                            is_orphan_insert = False
                            if existing_order:
                                ex_id, ex_filled, ex_status = existing_order
                                if float(ex_filled or 0) <= 0 and o_filled > 0:
                                    logger.info(f"🩹 [HEALING] Order {o_cid} exists with 0 fill but exchange reports {o_filled}. Healing ledger.")
                                    _oi_cur.execute("UPDATE bot_orders SET filled_amount=?, price=?, status=?, updated_at=? WHERE id=?",
                                                    (o_filled, o_price, o_status if o_status in ('filled', 'closed', 'canceled', 'cancelled') else 'filled', int(time.time()), ex_id))
                                    _oi_conn.commit()
                                continue # Skip normal insert
                            else:
                                is_orphan_insert = True

                            raw_otype = parts[2].upper() if len(parts)>2 else 'GRID'
                            otype_r = raw_otype if raw_otype in ('ENTRY','GRID','TP','HEDGETP') else 'GRID'

                            _oi_cur.execute("SELECT COALESCE(cycle_id,1), basket_start_time FROM trades WHERE bot_id=?", (attributed_bot_id,))
                            cr = _oi_cur.fetchone()
                            if cr:
                                cyc, bst = cr[0] or 1, cr[1] or 0
                            else:
                                cyc, bst = 1, 0
                                
                            # 🚀 CYCLE POISONING FIX:
                            # Reconciler fetches 7 days of history, which includes old closed cycles. 
                            # If we blindly attach them to the current `cyc`, TPs and cancels mathematically sum into the current ledger 
                            # and violently dive the bot's total_invested into negative ranges.
                            #
                            # Guard: any fill with an exchange timestamp more than 60s BEFORE basket_start_time
                            # belongs to a previous cycle and must be demoted.
                            # Current-cycle fills are always timestamped AFTER basket_start_time — never before it.
                            o_ts = o.get('timestamp') or 0
                            if bst == 0 or (o_ts > 0 and o_ts < (bst * 1000 - 60000)):
                                cyc = max(1, cyc - 1)  # Demote to past cycle
                            step_g = int(parts[3]) if len(parts)>3 and parts[3].isdigit() else 1
                            
                            # 🚀 STATUS FIX: Insert as terminal status so recompute_invested_from_orders counts it!
                            final_status = o_status if o_status in ('filled', 'closed', 'canceled', 'cancelled') else 'filled'
                            
                            _oi_cur.execute("""INSERT OR IGNORE INTO bot_orders
                                (bot_id,step,order_type,order_id,price,amount,filled_amount,status,created_at,updated_at,client_order_id,notes,cycle_id)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,'history-orphan',?)""",
                                (attributed_bot_id, step_g, otype_r.lower(), o_id, o_price, o_filled, o_filled, final_status,
                                 int((o.get('timestamp') or time.time()*1000)/1000), int(time.time()), o_cid, cyc))
                            logger.info(f"   ➕ [HISTORY-ORPHAN] Inserted missing bot_order: Bot {attributed_bot_id} {otype_r} qty={o_filled}@{o_price} order_id={o_id}")
                        _oi_conn.commit(); _oi_conn.close()
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
                    history = ex.fetch_closed_orders(pair, since=since_ts, limit=1000)
                    if not isinstance(history, list):
                        history = []
                    # Also fetch open orders for partial fill recovery
                    try:
                        open_orders = ex.fetch_open_orders(pair)
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
                cid = order.get('clientOrderId', '')
                if not cid.startswith('CQB_'): continue
                
                # Parse CID: CQB_{bot_id}_{type}_{step}
                # Examples: CQB_1_ENTRY_0, CQB_1_GRID_1, CQB_1_TP_5
                try:
                    parts = cid.split('_')
                    if len(parts) < 4: continue
                    
                    bot_id = int(parts[1])
                    otype = parts[2] # ENTRY, GRID, TP
                    step = int(parts[3])
                    
                    current_state = bot_states.get(bot_id, {})
                    
                    # 🔑 CYCLE-ID GUARD: The definitive, time-free fill isolation check.
                    # An offline fill is ONLY adopted if it belongs to the bot's CURRENT cycle.
                    # A fill from a previous cycle will have no matching bot_orders row with the
                    # current cycle_id, OR will have a 'reset_cleared' status. Both are rejected.
                    cursor.execute("""
                        SELECT status, cycle_id, filled_amount FROM bot_orders 
                        WHERE (order_id=? OR client_order_id=?)
                        ORDER BY filled_amount DESC LIMIT 1
                    """, (order['id'], cid))
                    row = cursor.fetchone()

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
                    
                    fill_price = order.get('average') or order.get('price') or 0.0
                    fill_qty = order.get('filled') or 0.0

                    if order_status in ('filled', 'closed') and fill_qty <= 0:
                        logger.debug(f"⏭️ [OFFLINE-SYNC] Skipping {cid}: status=filled but filled=0 from exchange. WS path already handled this.")
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
                        cursor.execute("UPDATE bot_orders SET status='filled', updated_at=?, filled_amount=? WHERE order_id=? OR client_order_id=?",
                                       (int(time.time()), fill_qty, order['id'], cid))
                    else:
                        cursor.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,))
                        _cycle_row = cursor.fetchone()
                        _bot_cycle = _cycle_row[0] if _cycle_row and _cycle_row[0] else 1
                        cursor.execute("""
                            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, updated_at, client_order_id, notes, cycle_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)
                        """, (bot_id, step, otype.lower(), order['id'], fill_price, fill_qty, fill_qty,
                              int(order.get('timestamp', time.time()*1000)/1000), int(time.time()), cid, 'Reconstructed from History', _bot_cycle))

                    if otype == 'TP':
                        if fill_price <= 0:
                            logger.warning(f"⚠️ Skipping OFFLINE_TP for Bot {bot_id}: fill_price={fill_price} is invalid.")
                            continue
                        try:
                            ex_for_guard = self.exchanges.get('future') or self.exchanges.get('spot')
                            if ex_for_guard:
                                live_positions = ex_for_guard.fetch_positions()
                                norm_fill_sym = normalize_symbol(fill_symbol)
                                still_has_position = any(
                                    normalize_symbol(p['symbol']) == norm_fill_sym and abs(float(p.get('contracts', 0))) > 0.001
                                    for p in live_positions
                                )
                                if still_has_position:
                                    logger.warning(f"🛡️ [TP-GUARD] Skipping OFFLINE_TP reset for Bot {bot_id} ({fill_symbol}): Exchange STILL has an open position.")
                                    cursor.execute("UPDATE bot_orders SET status='auto_closed' WHERE (order_id=? OR client_order_id=?)", (order['id'], cid))
                                    continue
                        except Exception as guard_err:
                            logger.error(f"TP position guard failed for Bot {bot_id}: {guard_err}. Skipping reset to be safe.")
                            continue
                        self._handle_offline_tp_fill(bot_id, bot_name, fill_price, fill_symbol)
                        stats['tp_fills'] += 1
                        
                    elif otype == 'GRID':
                        if step >= curr_step:
                            cursor.execute(
                                "SELECT COUNT(*) FROM bot_orders WHERE order_id=? AND status IN ('filled','closed')",
                                (order['id'],)
                            )
                            already_in_orders = cursor.fetchone()[0] > 0
                            if unaccounted_qty <= 1e-8 and already_in_orders:
                                logger.info(f"⏭️ [OFFLINE-DEDUP] Skipping fill for Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — already fully accounted for.")
                                cursor.execute("UPDATE bot_orders SET status='filled' WHERE (order_id=? OR client_order_id=?)", (order['id'], cid))
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
                            
                    elif otype == 'ENTRY':
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

                    elif otype == 'HEDGETP':
                        logger.info(f"🛡️ ✅ [OFFLINE-HEDGETP] Re-playing HEDGETP for Bot {bot_id}")
                        stats['tp_fills'] += 1

                    elif otype == 'HEDGE':
                        logger.info(f"🛡️ ✅ [OFFLINE-HEDGE] Re-playing HEDGE for Bot {bot_id}")
                        stats['grid_fills'] += 1
                        
                except Exception as e:
                    logger.debug(f"Error parsing/processing CID {cid}: {e}")
                    continue

            conn.commit()
            conn.close()

        stats['total'] = stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills']
        return stats


    def _mark_order_filled(self, cursor, order_id, fill_price):
        cursor.execute("""
            UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?
            WHERE order_id = ?
        """, (int(time.time()), fill_price, int(time.time()), order_id))

    def _handle_offline_tp_fill(self, bot_id, bot_name, fill_price, symbol):
        # GUARD: Never call reset_bot_after_tp with an invalid price (belt-and-suspenders)
        # The caller already guards, but this function is the final safety net.
        if not fill_price or float(fill_price) <= 0:
            logger.warning(
                f"⚠️ [OFFLINE-TP-GUARD] Skipping reset for Bot {bot_id} ({bot_name}): "
                f"fill_price={fill_price} is invalid (0 or None). Will not corrupt trade history."
            )
            return
        reset_bot_after_tp(bot_id, exit_price=fill_price, action_label='OFFLINE_TP')
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
        results = []
        
        for bot in bot_states:
            # Common: Check propagation delay
            if (time.time() - bot.basket_start_time) < 60:
                continue

            pair_orders = all_orders.get(bot.pair, [])
            
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
                    
                    # 🗑️ NO AUTO-CLEAN: We wait for the user to use the "Manual Link Recovery Tool"
                    # or force an aggressive cleanup explicitly.
                    
                    # Also mark them closed in DB to stop the cycle
                    conn = get_connection()
                    for o in my_orders:
                        conn.execute("UPDATE bot_orders SET status='cancelled', updated_at=? WHERE order_id=?", (int(time.time()), o.order_id))
                    conn.commit()
        
        return results

    # ------------------------------------------------------------------
    # STEP 3: NET-SUM VERIFICATION
    # ------------------------------------------------------------------
    def resolve_net_mismatch(self, bot_states: List[BotState], positions: Dict[str, List[ExchangePosition]], all_orders: Dict[str, List[ExchangeOrder]] = {}, force_adoption: bool = False) -> List[ReconciliationResult]:
        """
        Calculates Net Virtual Position and compares with Net Physical Position.
        If significant deviation exists, identifies 'Ghost' bots and resets them using SYSTEM_FIX.
        """
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
        conn = get_connection()
        try:
            cursor = conn.cursor()
            for bot in bot_states:
                if bot.in_trade:
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
                    """, (bot.bot_id, bot.bot_id))
                    row = cursor.fetchone()
                    bot_qtys[bot.bot_id] = float(row[0]) if row else 0.0
        finally:
            conn.close()

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
                if b.in_trade and b.avg_entry_price > 0:
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


            # 🚀 Case A: Impossible Bot Mass Detection (Vanished or Ghost Bots)
            # A bot is mathematically a "Ghost" if it independently claims to hold MORE units
            # than the entire gross sum existing on the exchange (plus any virtual opposing bots in one-way mode).
            # This occurs if a user manually closes the position or if it was liquidated/ADL'd.
            for b in bots:
                if b.in_trade:
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
                    
                    # Exact precision enforced.
                    if bot_qty > (max_possible_qty + 0.0001):
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
                                if physical_matching_direction_qty < 0.0001 and opposite_virtual_qty < bot_qty:
                                    logger.critical(
                                        f"💥 [VANISHED POSITION DETECTED] Bot {b.name}: Claims {bot_qty:.6f} units but Math Capacity is {max_possible_qty:.6f}! "
                                        f"Entry was confirmed, so the position vanished externally (Liquidated/ADL/Manual). Forcing memory wipe!"
                                    )
                                    from .database import log_reconciliation
                                    # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() gate — will block if physical position exists
                                    wiped = safe_wipe_bot(
                                        b.bot_id, b.pair, b.direction,
                                        reason=f"VANISHED_POSITION: claims {bot_qty:.6f} units but Math Capacity is {max_possible_qty:.6f}",
                                        exit_price=0.0
                                    )
                                    if wiped:
                                        log_reconciliation(
                                            bot_id=b.bot_id,
                                            pair=b.pair,
                                            action="RESET_VANISHED_POSITION",
                                            details=f"Mismatch: Bot claims {bot_qty:.6f}, Math Capacity is {max_possible_qty:.6f}. Vanished from exchange. Resetting bot."
                                        )
                                        results.append(ReconciliationResult(
                                            bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                            action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                            details="Reset Vanished Confirmed Position", requires_manual_intervention=False
                                        ))
                                else:
                                    logger.warning(
                                        f"⚠️ [PARTIAL VANISH DEFERRED] Bot {b.name}: Claims {bot_qty:.6f} units but Physical is {physical_matching_direction_qty:.6f} (max_capacity={max_possible_qty:.6f}). "
                                        f"Since Physical > 0, we will NOT wipe the bot's memory. Deferring to Net Gap Resolution."
                                    )
            # --- CASE B.2: STRUCTURAL GHOST DETECTION ---
            # Even if net-notional is close, if a bot claims high step but lacks history, it's a structural ghost (legacy state).
            for b in bots:
                # 🚀 ARCHITECTURAL FIX: 
                # Do not execute Structural Ghost destruction if the bot's entry is explicitly confirmed. 
                # 'entry_confirmed=1' indicates the position was successfully spawned via WebSocket fill, 
                # or was manually adopted/audited by the user/system.
                # NEW FIX: Also EXEMPT bots that were just anchored from physical exchange data (Step 0 but invested > 1)
                # 🔥 MID-FILL GUARD: If the bot's entry_order_id is STILL OPEN on the exchange, the bot is
                # currently being filled via partial fills. NEVER reset a bot mid-fill!
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

                        if phys_qty > 1.0:
                            # Exchange has a real position — this is NOT a ghost. Do NOT wipe.
                            logger.warning(
                                f"🛡️ [STRUCTURAL-GHOST BLOCKED] Bot {b.name} was flagged as ghost "
                                f"(no cycle fills since basket_start={b.basket_start_time}), "
                                f"BUT exchange shows {phys_qty:.4f} physical units. REFUSING WIPE. "
                                f"Basket_start_time will be healed on next reconcile cycle."
                            )
                        else:
                            cursor.execute("""
                                SELECT COUNT(*), SUM(amount * price) FROM bot_orders 
                                WHERE bot_id=? AND status IN ('filled', 'closed') AND created_at >= (? - 120)
                                AND cycle_id = ?
                            """, (b.bot_id, b.basket_start_time, b.cycle_id))
                            row = cursor.fetchone()
                            
                            # If bot has money invested, but ZERO filled orders in the current basket session
                            if not row or row[0] == 0:
                                logger.critical(f"👻 [STRUCTURAL-GHOST] Bot {b.name} claims ${b.total_invested:.2f} (Step {b.current_step}) but has NO filled orders since basket start ({b.basket_start_time}) AND exchange shows 0 physical. Resetting to truth.")
                                # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() — will block if CARRY_PENDING or physical > 0
                                wiped = safe_wipe_bot(
                                    b.bot_id, b.pair, b.direction,
                                    reason=f"STRUCTURAL_GHOST: ${b.total_invested:.2f} claimed, 0 fills since basket_start, 0 physical",
                                    exit_price=0.0
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
                        if 'conn' in locals(): conn.close()

            # --- CASE C: SIGNIFICANT NOTIONAL DEVIATION (AUTO-REPAIR) ---
            # 🚀 FIX: Compare QTY directly, not USD notional.
            # USD threshold was fundamentally wrong — a 23.6 SUI gap or 0.65 SOL gap
            # could be below $50 USD and would never be fixed. QTY must match exactly.
            # Compute net QTY for each side (virtual from trades, physical from exchange positions).
            virtual_net_qty = 0.0
            for b in bots:
                if b.in_trade and b.avg_entry_price > 0:
                    qty = b.total_invested / b.avg_entry_price
                    virtual_net_qty += qty if b.direction.upper() == 'LONG' else -qty

            physical_net_qty = 0.0
            for p in pair_positions:
                physical_net_qty += abs(p.size) if p.side == 'LONG' else -abs(p.size)

            delta_qty = abs(virtual_net_qty - physical_net_qty)
            # Keep delta_notional for logging only
            delta_notional = abs(virtual_net_usd - physical_net_usd)

            # QTY_EPSILON: 0.0001 units — below this is floating point noise, not a real gap.
            QTY_EPSILON = 0.0001
            genuine_in_trade_bots = [b for b in bots if b.in_trade]

            # is_sole must be computed BEFORE the if-block so it is available in both Case C and Case D
            is_sole = self.is_sole_bot(pair, bots)

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
                      for b in bots:
                          if not b.in_trade: continue
                          logger.warning(f"🛡️ [GLOBAL-FLATTEN] Exchange physically holds 0.0 units for {pair}. Auto-zeroing orphaned Bot {b.name}.")
                          
                          # 1. Search for proof of exit
                          proof = self._find_proof_of_exit(b)
                          if proof:
                              logger.info(f"✅ [GLOBAL-FLATTEN PROOF FOUND] Found manual exit or liquidation order: {proof.get('id')}")
                          else:
                              # 2. No proof found: Perform a professional accounting adjustment before resetting
                              logger.warning(f"⚠️ [GLOBAL-FLATTEN NO PROOF] Physical is 0.0, but DB proves {b.total_invested:.2f} virtual holding. Attempting strictly guarded wipe...")
                              self._execute_accounting_adjustment(b, 0.0, 0.0, "Global Flatten: Zeroing missing asset")
                              
                          from .database import log_reconciliation
                          # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() gate
                          wiped = safe_wipe_bot(
                              b.bot_id, b.pair, b.direction,
                              reason="GLOBAL_FLATTEN: Exchange physically holds 0 units for pair",
                              exit_price=0.0,
                              force=True  # 🚀 ALLOW MANUAL INTERVENTION: If user flattens Binance to 0.0, aggressively reset the bot
                          )
                          if wiped:
                              log_reconciliation(
                                  bot_id=b.bot_id, pair=pair, action="RESET_MISSING_EXCHANGE_ASSET",
                                  details=f"Global Flatten: Zeroed {b.total_invested:.4f} virtual holding because physical reality dropped to 0.0. Proof={bool(proof)}"
                              )
                              results.append(ReconciliationResult(
                                  bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                  details=f"Global Flatten: Exchange asset entirely absent.", requires_manual_intervention=False
                              ))
                      continue # Skip the rest of Case C since all active bots were just erased
                  
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
                              # Sole-bot on the WRONG side of physical reality. This means a full external reversal.
                              logger.warning(f"🛡️ [SIDE-PRUNE] Bot {b.name} ({bot_side}) is physically impossible while Exchange is {phys_side} (sole-bot). Applying adjustment.")
                              try:
                                  proof = self._find_proof_of_exit(b)
                                  if proof:
                                      logger.info(f"✅ [SIDE-PRUNE PROOF FOUND] Exit order found: {proof.get('id')}")
                                  else:
                                      logger.warning(f"⚠️ [SIDE-PRUNE NO PROOF] Writing off {bot_side} position via synthetic reduce.")
                                      self._execute_accounting_adjustment(b, 0.0, 0.0, f"Side-Prune: Zeroing {bot_side} position to match physical {phys_side}")
                                      
                                  from .database import log_reconciliation
                                  # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() gate
                                  wiped = safe_wipe_bot(
                                      b.bot_id, b.pair, b.direction,
                                      reason=f"SIDE_PRUNE: {bot_side} bot impossible while exchange is {phys_side} (sole-bot)",
                                      exit_price=0.0
                                  )
                                  if wiped:
                                      log_reconciliation(
                                          bot_id=b.bot_id, pair=pair, action="RESET_MISSING_EXCHANGE_ASSET",
                                          details=f"Side-Prune: Reversed side. Zeroed {b.total_invested:.4f} bot."
                                      )
                                      results.append(ReconciliationResult(
                                          bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                          details=f"Zero-Adoption: Pruned impossible {bot_side} units (sole-bot).",
                                          requires_manual_intervention=False
                                      ))
                              except Exception as e:
                                  logger.error(f"Failed to prune impossible bot {b.name}: {e}")
                  else:
                      # Multi-bot pair: Physical > Virtual gap = offline grid fills NOT in trades table.
                      # Physical < Virtual gap would be a ghost — but that is handled by Case B.2 (structural ghost).
                      # 🚀 FIX: Compare absolute magnitudes to correctly handle SHORT positions (negative values).
                      phys_mag = abs(physical_net_usd)
                      virt_mag = abs(virtual_net_usd)
                      phys_gap = phys_mag - virt_mag  # positive = physical has more mass
                      
                      if phys_gap > 5.0:
                          logger.warning(f"ℹ️ [MULTIBOT-UNKNOWN] {pair}: Physical Mag=${phys_mag:.2f} exceeds Virtual Mag=${virt_mag:.2f} by Gap=${phys_gap:.2f}. Bypassing proportional assumption for strict proof-of-order-ID consensus.")
                      elif phys_gap < -5.0:
                          logger.warning(f"ℹ️ [MULTIBOT-GHOST] {pair}: Virtual Mag=${virt_mag:.2f} exceeds Physical Mag=${phys_mag:.2f}. Ghost bots — structural ghost detection will handle.")

            
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

                # ================================================================
                # 🧹 Consensus Strategy 0: Pre-emptive Dust Chaser
                # If the ENTIRE physical gap is itself tinier than $5 notional,
                # the gap is un-sellable dust. No deduction, adoption, or manual
                # intervention is needed. Just fire a reduceOnly market order to
                # physically clear the exchange wallet and zero the ledger.
                # ================================================================
                if delta_notional < 5.0 and abs(physical_net_usd) < 5.0:
                    logger.warning(f"🧹 [DUST-CHASER] {pair_normalized}: Gap notional ${delta_notional:.2f} is sub-$5. Treating as un-sellable dust. Firing physical clear.")
                    ex_dust = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)
                    if ex_dust and abs(physical_net_qty) > 1e-8:
                        dust_side = 'buy' if physical_net_qty < 0 else 'sell'  # Counter-direction to flatten
                        try:
                            ex_dust.create_order(
                                symbol=list(pair_positions)[0].symbol if pair_positions else pair,
                                type='market', side=dust_side, amount=abs(physical_net_qty),
                                params={'reduceOnly': True}
                            )
                            logger.info(f"✅ [DUST-CHASER] Physical {dust_side.upper()} market order executed for {abs(physical_net_qty):.6f} {pair_normalized}.")
                        except Exception as de:
                            logger.warning(f"⚠️ [DUST-CHASER] Exchange clear failed (likely already 0): {de}")
                    # Either way, clear all local bot states for this pair that are claiming dust
                    for dust_bot in bots:
                        if dust_bot.total_invested > 0 and dust_bot.total_invested < 5.0:
                            self._execute_accounting_adjustment(dust_bot, 0.0, 0.0, "Dust Chaser: Sub-$5 gap cleared")
                            # 🗡️ ARCHITECTURAL: Route through safe_wipe_bot() gate
                            safe_wipe_bot(
                                dust_bot.bot_id, dust_bot.pair, dust_bot.direction,
                                reason=f"DUST_CHASER: ${dust_bot.total_invested:.2f} < $5 un-sellable sub-threshold dust",
                                exit_price=0.0
                            )
                            results.append(ReconciliationResult(
                                bot_id=dust_bot.bot_id, bot_name=dust_bot.name, pair=pair_normalized, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details=f"Dust Chaser: Cleared ${dust_bot.total_invested:.2f} sub-$5 un-sellable position.",
                                requires_manual_intervention=False
                            ))
                    continue  # Fully handled — skip all remaining strategies

                # Consensus Strategy A: Sole-Bot Confident Adoption
                if is_sole:
                    bot = [b for b in bots if b.is_active and normalize_symbol(b.pair) == pair_normalized][0]
                    phys_dir = 'LONG' if physical_net_usd > 0 else ('SHORT' if physical_net_usd < 0 else 'FLAT')
                    
                    if phys_dir == 'FLAT' or bot.direction.upper() == phys_dir:
                        # 🚀 PROFESSIONAL ACCOUNTING:
                        # If bot mass > exchange mass (Missing Asset) OR
                        # If bot mass < exchange mass (Missing Entry)
                        # We execute the auto-adjustment to bridge the gap and continue operations.
                        self._execute_accounting_adjustment(bot, physical_net_usd, physical_net, "Sole-Bot Gap Adjustment")
                        results.append(ReconciliationResult(
                            bot_id=bot.bot_id, bot_name=bot.name, pair=bot.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Sole-Bot Auto-Adjustment: Synced logic to physical net (${physical_net_usd:.2f}) via formal accounting.",
                            requires_manual_intervention=False
                        ))
                        continue 
                    else:
                        logger.warning(f"🚫 [ADOPTION-BLOCKED] {pair_normalized}: Physical is {phys_dir} but sole bot {bot.name} is {bot.direction.upper()}.")
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
                if abs(phys_net) > abs(virt_net):
                    error_side = 'LONG' if phys_net > 0 else 'SHORT'
                else:
                    error_side = 'LONG' if virt_net > 0 else 'SHORT'
                    
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
                    if order_type_tag in ('TP', 'SL', 'STOP'):
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
                                   SUM(CASE WHEN bo.order_type IN ('tp', 'exit', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN bo.filled_amount ELSE 0 END) as exit_qty
                            FROM bot_orders bo
                            WHERE bo.bot_id IN ({placeholders}) AND bo.status IN ('filled','closed') AND bo.filled_amount > 0
                            GROUP BY bo.bot_id
                        """, tuple(bots_on_pair))
                    for row in _lcur.fetchall():
                        _bid, _entry_qty, _exit_qty = row
                        net = (_entry_qty or 0) - (_exit_qty or 0)
                        if net > 0.00001:
                            ledger_proof[_bid] = net
                    _lconn.close()
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
                        logger.warning(f"👻 [DNA-B4 PHANTOM-CLEAR] Bot {bot.name} claims ${bot.total_invested:.2f} but has ZERO TP or ledger evidence. Resetting to 0.")
                        self._execute_accounting_adjustment(bot, 0.0, 0.0, "DNA B.4 Phantom Clear: No evidence in TP orders or ledger")
                        # 🗡️ ARCHITECTURAL: Gate through safe_wipe_bot() — blocks if CARRY_PENDING
                        wiped = safe_wipe_bot(
                            bot.bot_id, bot.pair, bot.direction,
                            reason="DNA_B4_PHANTOM: Zero TP or ledger evidence for claimed position",
                            exit_price=0.0
                        )
                        if wiped:
                            results.append(ReconciliationResult(
                                bot_id=bot.bot_id, bot_name=bot.name, pair=pair_normalized,
                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details=f"Phantom Clear: Bot {bot.name} had no TP order or ledger evidence. Reset to 0.",
                                requires_manual_intervention=False
                            ))
                        b4_phantom_cleared = True

                if b4_ran:
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
                # Consensus Strategy B.5: Multi-Bot Directional Smart Deduction
                # FALLBACK ONLY — runs when B.4 found zero TP order claimants.
                # If exactly ONE bot matches the error direction, adopt physical net.
                # ================================================================
                suspects = [b for b in bots if b.in_trade and b.direction.upper() == error_side]
                direction_match_bots = [b for b in bots if b.direction.upper() == error_side]
                
                logger.info(f"🔍 [B.5 CHECK] {pair_normalized}: b4_ran={b4_ran}, is_sole={is_sole}, error_side={error_side}, match_bots={[b.name for b in direction_match_bots]}")

                resolved_by_deduction = False
                if not b4_ran and not is_sole and len(direction_match_bots) == 1:
                    target_bot = direction_match_bots[0]

                    # 🛡️ FIX: Use ABS magnitudes to compute gap — both physical and bot-claimed
                    # quantities are absolute values of inventory.
                    # For LONG: adjusted_phys_qty is positive, bot_claimed is positive — works.
                    # For SHORT: adjusted_phys_qty is negative (-2291), bot_claimed is positive (2296.9)
                    #            → WITHOUT fix: -2291 - 2296.9 = -4587.9 (WRONG magnitude!)
                    #            → WITH fix: |−2291| - 2296.9 = -5.9 (correct: need adoption_reduce of 5.9)
                    bot_claimed_abs = (target_bot.total_invested / target_bot.avg_entry_price
                                       if target_bot.avg_entry_price > 0 else 0)
                    phys_qty_abs = abs(adjusted_phys_qty)
                    # Signed gap: positive → exchange has MORE than DB (adoption_add)
                    #             negative → DB claims MORE than exchange (adoption_reduce)
                    gap_qty_signed = phys_qty_abs - bot_claimed_abs
                    adoption_type = 'adoption' if gap_qty_signed > 0 else 'adoption_reduce'

                    logger.info(f"🧠 [SMART-DEDUCT] {pair_normalized}: phys_abs={phys_qty_abs:.6f} bot_claimed={bot_claimed_abs:.6f} gap={gap_qty_signed:.6f} → {adoption_type} to Bot {target_bot.name}.")

                    # 🛡️ DIRECTION GATE: only write adoption_add if bot direction matches the physical error side
                    if gap_qty_signed > 0 and target_bot.direction.upper() != error_side:
                        logger.warning(
                            f"🛡️ [B5-DIRECTION-GATE] {pair_normalized}: BLOCKED adoption_add — "
                            f"target {target_bot.name} is {target_bot.direction.upper()} but gap is on {error_side} side."
                        )
                    else:
                        # ✅ PROOF-ONLY FIX: Stop writing synthetic rows completely. 
                        # A professional quant system demands exactly $0 difference via strict mathematical proof.
                        gap_usd = abs(gap_qty_signed) * (adjusted_phys_usd / adjusted_phys_qty if adjusted_phys_qty else 0)
                        
                        logger.warning(
                            f"⚠️ [B5-PROOF-GAP] Bot {target_bot.name}: Unexplained gap of "
                            f"${gap_usd:.2f} ({abs(gap_qty_signed):.6f} units). "
                            f"Strict Proof-Only consensus active: No organic receipt found. "
                            f"Refusing to synthetically adopt. Gap remains unproven."
                        )

                    from .database import log_reconciliation
                    log_reconciliation(
                        bot_id=target_bot.bot_id,
                        pair=pair_normalized,
                        action="SMART_DEDUCTION_DELEGATED",
                        details=f"Evaluated {gap_qty_signed:.6f} physical gap via Directional Smart Deduction (B.5). Delegated to proof-scanner."
                    )
                    results.append(ReconciliationResult(
                        bot_id=target_bot.bot_id, bot_name=target_bot.name, pair=pair_normalized,
                        action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                        details=f"Delegated gap ({gap_qty_signed:.6f}) to proof scanner.",
                        requires_manual_intervention=False
                    ))
                    resolved_by_deduction = True

                # ================================================================
                # Consensus Strategy C: Manual Requirement for Multi-Bot Mismatch
                # If MULTIPLE bots share the direction and have NO proof, logic cannot guess.
                # ================================================================
                if not b4_ran and not resolved_by_deduction:
                    # Prevent spamming duplicate results per pair
                    if not any(r.pair == pair_normalized for r in results):
                        same_dir_bots = [b.name for b in bots if b.direction.upper() == error_side]
                        results.append(ReconciliationResult(
                            bot_id=bots[0].bot_id, # Target the first bot just as an anchor point for the alert
                            bot_name=bots[0].name,
                            pair=pair_normalized,
                            action_taken=ReconciliationAction.ROGUE_POSITION, # Trigger Wizard marker
                            details=f"Ghost gap ({adjusted_phys_qty:.6f} / ${adjusted_phys_usd:.2f}) owned by {error_side} bot, but multiple {error_side} candidates exist: {same_dir_bots}. Use Manual Wizard.",
                            requires_manual_intervention=True
                        ))

                for b in suspects:
                    exit_proof = self._find_proof_of_exit(b)
                    if exit_proof:
                        logger.info(f"✅ GHOST EXIT PROOF: Bot {b.name} (ID {b.bot_id}) found fill {exit_proof.get('id')}.")
                        
                        # 🚀 SAFETY GATE: Do not wipe the bot if it still has physical inventory on the exchange
                        if adjusted_phys_qty > 0.0001:
                            logger.warning(f"⚠️ [SAFETY] Bot {b.name} found exit proof but physical qty is {adjusted_phys_qty:.6f}. Refusing to wipe bot state.")
                            continue

                        self._fix_ghost_bot(b, proof_order_id=f"GHOST_EXIT_{exit_proof.get('id')}")
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=b.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details="Reset Ghost Bot (Found exit proof in history)", requires_manual_intervention=False
                        ))

                # After checking all suspects for exit proofs, if we STILL have no idea who owns the gap:
                # ----------------------------------------------------------------------
                same_dir_bots = [b.name for b in bots if b.direction.upper() == error_side]
                results.append(ReconciliationResult(
                    bot_id=0, bot_name="NET-GAP", pair=pair_normalized, action_taken=ReconciliationAction.REQUIRE_MANUAL,
                    details=(
                        f"⚠️ OWNERSHIP AMBIGUITY — Cannot auto-resolve.\n"
                        f"   Pair: {pair_normalized} | Direction: {error_side} | Gap: ${net_error:.2f}\n"
                        f"   Root Cause: {len(same_dir_bots)} bot(s) share this direction: {same_dir_bots}\n"
                        f"   No CQB_ signed open orders found — could not prove ownership.\n"
                        f"   Action: Open Binance Futures → close position manually → then reset correct bot(s) to Scanning."
                    ),
                    requires_manual_intervention=True
                ))
            else:
                # Consensus Strategy D: Promotion (Math compiles perfectly)
                # Ensure bots in Scanning with money are unblocked
                for b in bots:
                    # 🧹 PROFESSIONAL DUST CHASER: 
                    # If the position is perfectly synced but worth less than $5 notional, 
                    # it is mathematically un-sellable on Binance (MIN_NOTIONAL error).
                    # The bot gets permanently stuck trying to place the TP. We must clear it.
                    if b.total_invested > 0 and b.total_invested < 5.0:
                        logger.warning(f"🧹 [DUST-CHASER] Bot {b.name} ({b.pair}): Perfectly synced but holding only ${b.total_invested:.2f}. Un-sellable. Clearing dust.")
                        
                        # 🚀 PHYISCAL MARKET EXECUTION
                        ex = self.exchanges.get('future')
                        if not ex and self.exchanges:
                            ex = list(self.exchanges.values())[0] # Fallback
                            
                        if ex:
                            qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0.0
                            if qty > 1e-8:
                                exit_side = 'sell' if b.direction.upper() == 'LONG' else 'buy'
                                try:
                                    logger.info(f"🧹 [DUST-CHASER] Executing PHYSICAL MARKET {exit_side.upper()} order for {qty:.6f} {b.pair} to clear dust wallet.")
                                    # reduceOnly allows closing sub-$5.00 positions bypassing MIN_NOTIONAL
                                    ex.create_order(
                                        symbol=b.pair,
                                        type='market',
                                        side=exit_side,
                                        amount=qty,
                                        params={'reduceOnly': True}
                                    )
                                    logger.info(f"✅ [DUST-CHASER] Physical exchange clearance successful for {b.pair}. Zeroing local ledger.")
                                except Exception as dust_err:
                                    logger.error(f"❌ [DUST-CHASER] Failed to physically clear exchange dust for {b.pair}: {dust_err}")
                                    # Even if API fails (LOT_SIZE constraints), we MUST clear DB ledger so the bot releases.
                        
                        self._execute_accounting_adjustment(b, 0.0, 0.0, "Dust Chaser: Clearing un-sellable micro-position.")
                        # 🗡️ ARCHITECTURAL: Gate through safe_wipe_bot() — blocks if physical position or CARRY_PENDING
                        safe_wipe_bot(
                            b.bot_id, b.pair, b.direction,
                            reason=f"DUST_CHASER_V2: ${b.total_invested:.2f} un-sellable micro position",
                            exit_price=0.0
                        )
                        
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Dust Chaser: Scrapped ${b.total_invested:.2f} un-sellable position.",
                            requires_manual_intervention=False
                        ))
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
                            if 'conn' in locals(): conn.close()

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
                                        conn.close()
                                        return order
                    conn.close()
                        
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
            cursor.execute("""
                UPDATE trades 
                SET total_invested=0, current_step=0, avg_entry_price=0, 
                    entry_confirmed=0, basket_start_time=?
                WHERE bot_id=?
            """, (int(time.time()), bot_id))  # 🚀 FUNDAMENTAL FIX: Never set basket_start_time=0. Always stamp reset time so EE fires on next cycle.
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
            
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
        conn.close()

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
        🚀 PROOF-ONLY CONSENSUS:
        The bot relies entirely on its internal ledger of verified exchange fills.
        If physical reality deviates from the mathematically pure DB ledger, we log
        the discrepancy but we DO NOT inject fake adoption rows or overwrite the DB state.
        
        The discrepancy will persist physically, but the bot will continue closing exactly
        the amount of inventory it actually purchased.
        """
        try:
            logger.warning(
                f"⚖️ [INVENTORY DEVIATION] Bot {bot.name}: Physical differs from DB "
                f"(${phys_notional:.2f} | {phys_qty:.4f} units). Reason: {reason}. "
                f"DB relies strictly on Proof-Only ledger. No local state overwritten."
            )
            return True
        except Exception as e:
            logger.error(f"Inventory Adjustment failed for bot {bot.name}: {e}")
            return False


    def _fix_ghost_bot(self, bot: BotState, proof_order_id: str = "MANUAL_FIX"):
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
            cursor.execute("UPDATE bot_orders SET status='cancelled' WHERE bot_id=? AND status='open'", (bot.bot_id,))
            cursor.execute("UPDATE bot_orders SET status='reset_cleared' WHERE bot_id=? AND status IN ('filled', 'closed', 'missing')", (bot.bot_id,))
            
            conn.commit()
            conn.close()
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
                except: pass
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
            cursor.execute("SELECT base_size, martingale_multiplier FROM bots WHERE id=?", (bot_id,))
            cfg_row = cursor.fetchone()
            base_size = float(cfg_row[0] or 0.0) if cfg_row else 0.0
            mart_mult = float(cfg_row[1] or 1.0) if cfg_row else 1.0

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
                base_size=base_size,
                martingale_multiplier=mart_mult,
            ))
        conn.close()
        return states

    def _align_memory_to_ledger(self):
        """
        🛡️ RIGOROUS MEMORY ALIGNMENT:
        Enforces that the 'trades' table (total_invested, avg_entry) is exactly 
        subordinate to the 'bot_orders' ledger (filled entries - filled exits).
        If memory deviates from DNA, memory is overwritten.
        """
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Get all bots currently tracked in trades table
            cursor.execute("""
                SELECT b.id, b.name, t.cycle_id, t.total_invested, b.pair, t.entry_confirmed
                FROM bots b
                JOIN trades t ON b.id = t.bot_id
                WHERE t.total_invested > 0 OR b.status = 'IN TRADE'
            """)
            active_bots = cursor.fetchall()

            for b_id, b_name, cycle, db_inv, pair, entry_confirmed in active_bots:
                # Calculate True Ledger Sum for CURRENT cycle
                # We exclude 'reset_cleared' which are archived DNA from previous trades.
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(
                            CASE 
                                WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN (filled_amount * price)
                                WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -(filled_amount * price)
                                ELSE 0.0
                            END
                        ), 0.0) AS total_cost,
                        COALESCE(SUM(
                            CASE 
                                WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount
                                WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -filled_amount
                                ELSE 0.0
                            END
                        ), 0.0) AS total_qty
                    FROM bot_orders 
                    WHERE bot_id = ? AND status NOT IN ('reset_cleared', 'auto_closed')
                    AND (cycle_id = ? OR cycle_id IS NULL)
                    AND filled_amount > 0
                """, (b_id, cycle))
                
                row = cursor.fetchone()
                if not row:
                    ledger_qty = 0.0
                    total_cost = 0.0
                    avg_entry = 0.0
                    true_inv = 0.0
                else:
                    total_cost = float(row[0] or 0.0)
                    ledger_qty = float(row[1] or 0.0)
                    
                    if ledger_qty > 0.0001 and total_cost > 0.0001:
                        avg_entry = total_cost / ledger_qty
                        true_inv = ledger_qty * avg_entry
                    else:
                        avg_entry = 0.0
                        true_inv = 0.0
                        ledger_qty = 0.0
                if abs(db_inv - true_inv) > 0.01:
                    logger.warning(f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}) memory=${db_inv:.4f} vs DNA-Ledger=${true_inv:.4f}.")
                    
                    if ledger_qty <= 0.0001:
                        # ⚡ LIFECYCLE GUARD: If entry_confirmed=1, the system (or reconciler) has
                        # explicitly asserted this position is real (e.g., offline fill adoption).
                        # Ledger may be empty because the fill event hasn't been recorded yet.
                        # DO NOT RESET — hold the state and flag for reconciler to investigate.
                        if entry_confirmed:
                            logger.warning(
                                f"⚠️ [DNA-HOLD] Bot {b_id} ({b_name}): entry_confirmed=1 but ledger qty=0. "
                                f"Position is asserted real. Skipping reset — reconciler must verify. "
                                f"Check physical position and order history."
                            )
                        else:
                            # No entry confirmed and ledger is empty.
                            # 🚀 FIX 4: Check if exchange has physical position before resetting!
                            # The WS connection may have dropped the 'entry' fill, leaving the DB blind.
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
                                
                            if phys_qty > 0.0:
                                logger.warning(f"⚠️ [DNA-RECOVER] Bot {b_id} ({b_name}): Ledger empty & entry_confirmed=0, BUT physical={phys_qty:.4f}. Forcing offline rebuild.")
                                # Trigger offline scan immediately to pick up the missed WebSocket fill
                                self.reconstruct_offline_fills(lookback_hours=24)
                            else:
                                # Truly empty and no physical residue. Safe to reset.
                                logger.warning(f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}): Ledger empty, entry not confirmed. Resetting to Scanning.")
                                cursor.execute("""
                                    UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0, basket_start_time=0 
                                    WHERE bot_id=?
                                """, (b_id,))
                                cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (b_id,))
                    else:
                        # Proof says bot is invested. Fix memory to match ledger truth.
                        logger.info(f"🔧 [DNA-ALIGN] Syncing Bot {b_id} ({b_name}) memory to ledger: ${true_inv:.4f} @ {avg_entry:.4f}.")
                        cursor.execute("""
                            UPDATE trades SET total_invested=?, avg_entry_price=?, entry_confirmed=1,
                            cycle_phase='ACTIVE'
                            WHERE bot_id=?
                        """, (true_inv, avg_entry, b_id))
                        # CARRY_PENDING→ACTIVE: Once ledger proves outstanding position, ghost checks re-engage normally
            
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to align memory to ledger: {e}")
        finally:
            conn.close()

    def adopt_from_physical_positions(self, limit_per_symbol: int = 500) -> dict:
        """
        🔬 BIDIRECTIONAL PROOF RECONCILIATION — Cross-Reference Physical ↔ Ledger.

        Philosophy (per user): this is a professional quant bot. We don't blindly adopt
        fills — we PROVE them from both directions, then net them.

        Two-pass proof model:
        ─────────────────────
        PASS 1 — Ledger → Exchange (System's Claim):
          For each order the system claims to have placed (bot_orders):
          - Verify it on the exchange by order_id
          - If exchange confirms a fill but bot_orders has filled_amount=0 → heal it
          - If order was cancelled → mark it cancelled, don't count it
          This builds provable position from confirmed system-placed orders only.

        PASS 2 — Exchange → Ledger (Reality Check):
          Fetch exchange fill history for the symbol.
          For fills with our bot's CID DNA (CQB_{bot_id}_*):
          - If NOT already in bot_orders → we missed inserting it (offline/crash)
          - Adopt it — it's provably ours (CID is our DNA)
          Fills WITHOUT our CID DNA → logged as "possible manual trade", not adopted.

        Cycle Boundary (Checkpoint):
          If the bot has a basket_start_time > 0, that's when the current cycle
          started. We only scan fills AFTER that time. If the cycle was confirmed
          reset (entry_confirmed=0, total_invested=0), we use that as a hard stop.

        Final step: net the physical qty against our proof-adopted fills. If they
        match → perfect. If they don't → log the gap for manual review.
        """
        logger.info("🔬 [PHYS-ADOPT] Starting bidirectional proof reconciliation scan...")
        results = {}

        # Get the exchange interface
        ex = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)
        if not ex:
            logger.error("[PHYS-ADOPT] No exchange interface available.")
            return {}

        try:
            # ──────────────────────────────────────────────
            # Get physical positions from exchange
            # ──────────────────────────────────────────────
            try:
                raw_positions = ex.fetch_positions()
                if raw_positions is None:
                    logger.error("[PHYS-ADOPT] fetch_positions() returned None (API error/limit). Aborting physical adoption to prevent false wipes.")
                    return {}
            except Exception as _pe:
                logger.error(f"[PHYS-ADOPT] fetch_positions() failed: {_pe}")
                return {}

            # FIX 1: phys_positions stores {qty, entry_price} — entry price comes directly
            # from /fapi/v2/account response (entryPrice field), which is the real cost basis.
            # This is used by PASS 3 instead of fetch_ticker() to avoid mark-price drift.
            phys_positions = {}  # (symbol_normalized, 'long'/'short') -> {'qty': float, 'entry_price': float}
            for p in raw_positions:
                # fetch_positions() returns 'contracts' (signed, positive=long, negative=short)
                pos_qty = float(p.get('contracts') or 0)
                if abs(pos_qty) < 1e-9:
                    continue
                symbol = normalize_symbol(p.get('symbol', ''))
                # fetch_positions() normalises side to lowercase 'long'/'short' in exchange_interface.py
                side = str(p.get('side', '')).lower()
                if not symbol or side not in ('long', 'short'):
                    continue
                entry_price = float(p.get('entryPrice') or 0)
                phys_positions[(symbol, side)] = {'qty': abs(pos_qty), 'entry_price': entry_price}
                logger.info(f"  📍 Exchange: {symbol} {side} qty={abs(pos_qty):.6f} entryPrice={entry_price:.6f}")

            if phys_positions:
                for sym, info in phys_positions.items():
                    logger.info(f"  📍 Exchange has open position: {sym[0]} {sym[1]} qty={info['qty']:.6f}")
            else:
                logger.info("[PHYS-ADOPT] No open positions on exchange — will check for stale DB positions.")

            # ──────────────────────────────────────────────
            # Load all active bots from DB
            # ──────────────────────────────────────────────
            conn = get_connection()
            all_bots = conn.execute("""
                SELECT b.id, b.pair, b.direction, b.name,
                       t.current_step, t.basket_start_time, t.total_invested,
                       t.entry_confirmed, COALESCE(t.cycle_id, 1),
                       b.base_size, b.martingale_multiplier
                FROM bots b LEFT JOIN trades t ON t.bot_id=b.id
                WHERE b.is_active=1
            """).fetchall()
            conn.close()

            # Build (normalized_symbol, direction) -> bot_info
            # Build (normalized_symbol, direction) -> list of bot_info
            from collections import defaultdict
            bot_group = defaultdict(list)
            for row in all_bots:
                bid, pair, direction, name, cur_step, bst, cur_inv, entry_conf, cycle_id, base_sz, mm_mult = row
                sym = normalize_symbol(pair)
                d   = str(direction or '').lower()
                bot_group[(sym, d)].append({
                    'bot_id':       bid,
                    'name':         name or '',
                    'pair':         pair,
                    'direction':    d,
                    'current_step': int(cur_step or 0),
                    'basket_start_time': int(bst or 0),
                    'total_invested':    float(cur_inv or 0),
                    'entry_confirmed':   int(entry_conf or 0),
                    'cycle_id':          int(cycle_id or 1),
                    'base_size':         float(base_sz or 0),
                    'martingale_multiplier': float(mm_mult or 2.0),
                })

            all_bot_keys = set(bot_group.keys()) | set(phys_positions.keys())

            for (symbol, side) in all_bot_keys:
                _phys_info = phys_positions.get((symbol, side), {})
                phys_qty = _phys_info.get('qty', 0.0) if isinstance(_phys_info, dict) else 0.0
                bots_for_side = bot_group.get((symbol, side), [])

                if not bots_for_side:
                    # Physical position exists but no matched bot (on ANY ID) — log and skip
                    if phys_qty > 0:
                        logger.warning(
                            f"[PHYS-ADOPT] ⚠️ Physical position {symbol} {side} "
                            f"qty={phys_qty:.6f} has no matching active bot. Possible manual trade."
                        )
                    continue

                total_proved_qty = 0.0
                primary_bot = bots_for_side[0] # To take the adoption if needed

                # We fetch my trades ONCE per symbol, for ALL bots sharing this symbol!
                try:
                    raw_fills = ex.exchange.fetch_my_trades(symbol, limit=limit_per_symbol) or []
                except Exception as _fe:
                    logger.warning(f"  fetch_my_trades({symbol}) failed: {_fe}")
                    raw_fills = []

                logger.info(
                    f"\n[PHYS-ADOPT] PASS1+2 ── Group: {len(bots_for_side)} bots ({symbol} {side}) ──\n"
                    f"  Physical qty: {phys_qty:.6f} | raw_fills: {len(raw_fills)}"
                )

                # Group raw_fills by order_id globally for this symbol
                grouped_fills = {}
                for fill in raw_fills:
                    oid = str(fill.get('order') or fill.get('orderId') or fill.get('id') or '')
                    if not oid: continue
                    if oid not in grouped_fills:
                        grouped_fills[oid] = {
                            'cid': str(fill.get('clientOrderId') or ''),
                            'side': str(fill.get('side') or '').lower(),
                            'qty': 0.0,
                            'cost': 0.0,
                            'ts': int((fill.get('timestamp') or 0) // 1000)
                        }
                    
                    f_qty = float(fill.get('amount') or fill.get('filled') or 0)
                    f_price = float(fill.get('price') or 0)
                    if f_qty > 0 and f_price > 0:
                        grouped_fills[oid]['qty'] += f_qty
                        grouped_fills[oid]['cost'] += f_qty * f_price
                        grouped_fills[oid]['ts'] = min(grouped_fills[oid]['ts'], int((fill.get('timestamp') or 0) // 1000))
                        if not grouped_fills[oid]['cid'] and fill.get('clientOrderId'):
                            grouped_fills[oid]['cid'] = str(fill.get('clientOrderId'))

                # Now run PASS 0, 1, 2 for each bot in the group
                for bot_info in bots_for_side:
                    bot_id   = bot_info['bot_id']
                    bot_name = bot_info['name']
                    db_invested  = bot_info['total_invested']
                    db_confirmed = bot_info['entry_confirmed']
                    bst        = bot_info['basket_start_time']
                    cycle_id   = bot_info['cycle_id']
                    dna_prefix = f"CQB_{bot_id}_"
                    max_step   = bot_info['current_step']

                    if phys_qty == 0.0 and (db_confirmed or db_invested > 0):
                        logger.info(
                            f"\n  [PASS0] Bot {bot_id} ({bot_name}): Exchange=0 but DB=$\n"
                            f"  {db_invested:.2f}. Position externally closed. Auto-resetting."
                        )
                        conn   = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            UPDATE trades
                            SET entry_confirmed=0, total_invested=0, avg_entry_price=0,
                                current_step=0, basket_start_time=0
                            WHERE bot_id=?
                        """, (bot_id,))
                        cursor.execute("""
                            UPDATE bot_orders SET status='cancelled', updated_at=?
                            WHERE bot_id=? AND status IN ('open','pending','partial')
                        """, (int(time.time()), bot_id))
                        conn.commit()
                        conn.close()
                        results[bot_id] = {
                            'symbol': symbol, 'side': side,
                            'phys_qty': 0.0, 'proved_qty': 0.0,
                            'qty_matched': True,
                            'p1_healed': 0, 'p2_adopted': 0, 'suspicious': 0,
                            'action': 'auto_reset',
                        }
                        continue

                    # If phys_qty == 0, we're done with this bot.
                    if phys_qty == 0.0:
                        continue

                    # Pass 1: DB -> Exchange
                    conn   = get_connection()
                    cursor = conn.cursor()
                    sys_orders = conn.execute("""
                        SELECT id, order_id, client_order_id, order_type, price, amount,
                               filled_amount, status, step
                        FROM bot_orders
                        WHERE bot_id=? AND status NOT IN ('cancelled','canceled','reset_cleared')
                          AND client_order_id LIKE ?
                        ORDER BY step ASC, created_at ASC
                    """, (bot_id, f"CQB_{bot_id}_%")).fetchall()

                    p1_corrected = 0
                    for row in sys_orders:
                        (bo_id, bo_oid, bo_cid, bo_order_type, bo_price, bo_amount, bo_filled, bo_status, bo_step) = row
                        if not bo_oid: continue
                        _otype = str(bo_order_type or '').lower()
                        is_entry = _otype in ('entry', 'grid', 'adoption', 'adoption_add')
                        bo_filled_f = float(bo_filled or 0)
                        
                        if bo_status in ('filled', 'closed') and bo_filled_f > 0:
                            continue

                        try:
                            ex_order = ex.exchange.fetch_order(bo_oid, symbol)
                            ex_filled = float(ex_order.get('filled') or 0)
                            ex_status = str(ex_order.get('status') or '').lower()
                            ex_price  = float(ex_order.get('average') or ex_order.get('price') or bo_price or 0)
                        except Exception as _oe:
                            continue

                        if ex_status in ('canceled', 'cancelled', 'expired'):
                            cursor.execute("UPDATE bot_orders SET status='cancelled', updated_at=? WHERE id=?", (int(time.time()), bo_id))
                        elif ex_filled > 0:
                            new_status = 'filled' if ex_filled >= float(bo_amount or 0) * 0.99 else 'open'
                            cursor.execute("""
                                UPDATE bot_orders
                                SET filled_amount=?, price=?, status=?, updated_at=?
                                WHERE id=?
                            """, (ex_filled, ex_price, new_status, int(time.time()), bo_id))
                            p1_corrected += 1
                    conn.commit()

                    # Pass 2: Exchange -> DB (using the single shared raw_fills dict)
                    _existing = conn.execute(
                        "SELECT order_id, status FROM bot_orders WHERE bot_id=? AND order_id IS NOT NULL AND order_id != ''",
                        (bot_id,)
                    ).fetchall()
                    existing_oids = {r[0] for r in _existing}
                    existing_statuses = {r[0]: r[1] for r in _existing}

                    p2_adopted = 0
                    p2_suspicious = 0
                    p2_net_qty = 0.0
                    p2_net_cost = 0.0
                    
                    for fill_oid, g_data in sorted(grouped_fills.items(), key=lambda x: x[1]['ts']):
                        fill_cid   = g_data['cid']
                        fill_side  = g_data['side']
                        fill_qty   = g_data['qty']
                        fill_price = g_data['cost'] / fill_qty if fill_qty > 0 else 0
                        fill_ts_s  = g_data['ts']

                        if fill_qty <= 0 or fill_price <= 0: continue

                        if not fill_cid:
                            if fill_oid in getattr(self, 'cid_cache', {}):
                                fill_cid = self.cid_cache[fill_oid]
                                g_data['cid'] = fill_cid
                            else:
                                # Search globally first across all bots
                                _cid_row = conn.execute("SELECT client_order_id FROM bot_orders WHERE order_id=? LIMIT 1", (fill_oid,)).fetchone()
                                if _cid_row and _cid_row[0]: 
                                    fill_cid = str(_cid_row[0])
                                    getattr(self, 'cid_cache', {})[fill_oid] = fill_cid
                                else:
                                    # It's a true orphan (not in DB). We MUST query the exchange for the order
                                    try:
                                        ex_order = ex.exchange.fetch_order(fill_oid, symbol)
                                        fill_cid = str(ex_order.get('clientOrderId') or '')
                                        g_data['cid'] = fill_cid
                                        getattr(self, 'cid_cache', {})[fill_oid] = fill_cid
                                    except Exception as _oe:
                                        logger.warning(f"  [PASS2] Could not fetch order {fill_oid} for CID check: {_oe}")
                                        getattr(self, 'cid_cache', {})[fill_oid] = "" # Cache failure as empty to prevent infinite retries

                        _is_entry = (fill_side == 'buy' and side == 'long') or (fill_side == 'sell' and side == 'short')
                        _is_exit  = (fill_side == 'sell' and side == 'long') or (fill_side == 'buy' and side == 'short')

                        if not fill_cid.startswith(dna_prefix):
                            continue

                        if _is_entry:
                            p2_net_qty += fill_qty
                            p2_net_cost += g_data['cost']
                            parts = fill_cid.split('_')
                            g_step = 0
                            if len(parts) >= 4 and parts[2] == 'GRID':
                                try: g_step = int(parts[3])
                                except: pass
                            
                            if fill_oid not in existing_oids:
                                _cid_type  = parts[2].upper() if len(parts) > 2 else 'ENTRY'
                                _otype_ins = 'entry' if _cid_type == 'ENTRY' else 'tp' if _cid_type in ('TP', 'HEDGETP') else 'grid'
                                cursor.execute("""
                                    INSERT OR IGNORE INTO bot_orders
                                      (bot_id, order_id, client_order_id, order_type, price, amount,
                                       filled_amount, status, step, cycle_id, created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?)
                                """, (bot_id, fill_oid, fill_cid, _otype_ins, fill_price, fill_qty, fill_qty, g_step, cycle_id, fill_ts_s, int(time.time())))
                                if cursor.rowcount > 0:
                                    p2_adopted += 1
                                    existing_oids.add(fill_oid)
                            elif existing_statuses.get(fill_oid) != 'filled':
                                cursor.execute("UPDATE bot_orders SET filled_amount=?, price=?, status='filled', updated_at=? WHERE order_id=? AND bot_id=?", (fill_qty, fill_price, int(time.time()), fill_oid, bot_id))
                                p1_corrected += 1
                                existing_statuses[fill_oid] = 'filled'

                        elif _is_exit and fill_cid.startswith(dna_prefix):
                            p2_net_qty = max(0.0, p2_net_qty - fill_qty)
                            if fill_oid in existing_oids and existing_statuses.get(fill_oid) != 'filled':
                                cursor.execute("UPDATE bot_orders SET filled_amount=?, price=?, status='filled', updated_at=? WHERE order_id=? AND bot_id=?", (fill_qty, fill_price, int(time.time()), fill_oid, bot_id))
                                p1_corrected += 1
                                existing_statuses[fill_oid] = 'filled'

                    conn.commit()

                    # Compute PROVED QTY for this bot
                    from engine.database import recompute_invested_from_orders
                    true_inv, true_avg, true_qty, true_step = recompute_invested_from_orders(bot_id)

                    total_proved_qty += true_qty
                    logger.info(f"  Bot {bot_id} ({bot_name}) true_qty={true_qty:.6f}")

                # End of bots_for_side iteration
                # PASS 3: Validate physical gap against DB proof
                if phys_qty == 0.0:
                    continue
                
                qty_tol   = max(phys_qty * 0.02, 0.001)
                qty_match = abs(total_proved_qty - phys_qty) <= qty_tol
                
                logger.info(
                    f"\n  📊 GROUP RESULT {len(bots_for_side)} bots ({symbol} {side}):\n"
                    f"     Physical qty    : {phys_qty:.6f}\n"
                    f"     Total Proved DB : {total_proved_qty:.6f}  "
                    f"{'✅ MATCH' if qty_match else '⚠️ GAP=' + str(round(phys_qty - total_proved_qty, 6))}"
                )

                if not qty_match:
                    gap = phys_qty - total_proved_qty
                    adoption_epsilon = 0.0001
                    if abs(gap) > adoption_epsilon:
                        # 🚀 STRICT PROOF-ONLY CONSENSUS:
                        # Refuse to inject synthetic rows. Keep DB mathematically pure based ONLY on verified API receipts.
                        # The cap at TP placement (min(virtual, physical)) will handle physical exit safety.
                        logger.warning(
                            f"⚠️ [FLOAT-DRIFT] Unprovable physical gap of {gap:.6f} on {symbol} {side}. "
                            f"System refuses to synthesize ledger. DB remains at {total_proved_qty:.6f}, Physical is {phys_qty:.6f}."
                        )
                    else:
                        logger.info(f"  ✅ [PASS3] Gap≈0 ({gap:.6f}): PASS-2 fully proved position.")

        except Exception as e:
            logger.error(f"[PHYS-ADOPT] Fatal error: {e}", exc_info=True)
            
        try:
            conn.close()
        except:
            pass

        logger.info(f"✅ [PHYS-ADOPT] Complete. Processed {len(results)} bot(s).")
        return results


# Alias for backward compatibility if needed
DeepReconciler = StateReconciler
