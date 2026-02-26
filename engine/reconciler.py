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
    update_martingale_step, log_reconciliation,
    DB_PATH
)
from .exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logger = logging.getLogger("StateReconciliation")


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
        
    def get_exchange(self, market_type: str):
        if market_type in self.exchanges:
            return self.exchanges[market_type]
        if not self.exchanges:
            return None
        return list(self.exchanges.values())[0]

    # ------------------------------------------------------------------
    # STEP 1: OFFLINE FILL DETECTION
    # ------------------------------------------------------------------
    def detect_offline_fills(self, since_hours: int = 48) -> Dict[str, int]:
        """
        Scans exchange history for orders that filled while we were offline.
        Updates the DB immediately so subsequent checks see the correct state.
        Refactored to be ROBUST: Uses Client Order ID parsing to reconstruct state.
        """
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total': 0}
        
        # 1. Identify pairs to scan (Active Bots + Open Orders)
        conn = get_connection()
        cursor = conn.cursor()
        
        # Get all bots to know what pairs to check
        cursor.execute("SELECT id, pair, name, status FROM bots WHERE is_active=1")
        active_bots = cursor.fetchall() # [(id, pair, name, status), ...]
        
        # Get pairs from open orders too
        cursor.execute("SELECT DISTINCT pair from bots WHERE id IN (SELECT DISTINCT bot_id FROM bot_orders WHERE status='open')")
        order_pairs = [r[0] for r in cursor.fetchall()]
        
        pairs_to_check = set([b[1] for b in active_bots] + order_pairs)
        
        # Pre-fetch Bot States for fast lookups
        # Map: bot_id -> {current_step, total_invested, avg_entry, basket_start_time}
        bot_states = {}
        cursor.execute("SELECT bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time FROM trades")
        for row in cursor.fetchall():
            bot_states[row[0]] = {
                'current_step': row[1], 'total_invested': row[2], 
                'avg_entry': row[3], 'entry_confirmed': row[4],
                'basket_start_time': row[5] or 0
            }

        conn.close() # Close mainly to keep scope clean, we'll reopen for updates

        # 2. Scan History per Pair
        since_ts = int((time.time() - (since_hours * 3600)) * 1000)
        
        for pair in pairs_to_check:
            # Find which exchange handles this pair (usually 'future')
            # For now assume 'future' or check config. 
            # In a mixed setup, we might need to check both if not sure.
            # But usually pair names are unique enough or we iterate exchanges.
            
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    # Fetch History
                    history = ex.fetch_closed_orders(pair, since=since_ts, limit=100)
                    if not isinstance(history, list):
                        if history is not None:
                            logger.debug(f"⚠️ Unexpected history format for {pair}: {history}")
                        continue
                    
                    # Sort by time (Oldest first) to replay history
                    history.sort(key=lambda x: x['timestamp'])
                    
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
                            
                            # 🚀 SESSION-SYNC FIX: Strictly ignore history before the current session's start time (epoch).
                            # This prevents re-playing old history into a new DB after a reset.
                            # basket_start_time is in seconds, order timestamp is in MS.
                            bot_epoch_ms = (current_state.get('basket_start_time', 0)) * 1000
                            order_time_ms = order.get('timestamp', 0)
                            
                            if bot_epoch_ms > 0 and order_time_ms < bot_epoch_ms:
                                # Order happened BEFORE this bot was started/reset. Skip it.
                                # Unless we are in an explicit adoption mode, we stick to the epoch.
                                continue
                            
                            # 🛡️ FATAL BUG FIX: If basket_start_time is 0 (Clean Wipe), Binance FAPI still returns
                            # 48 hours of history. We MUST reject any history older than 5 minutes to prevent
                            # ingesting 27-hour old closed orders as "brand new offline trades".
                            if bot_epoch_ms == 0:
                                try:
                                    # Format: CQB_{bot_id}_{type}_{step}_{timestamp_seconds}
                                    if len(parts) >= 5:
                                        cid_timestamp = int(parts[4])
                                        if (time.time() - cid_timestamp) > 300: # 5 minutes
                                            logger.debug(f"🛑 [OFFLINE-SYNC] Rejecting ANCIENT offline fill {cid} (Age: {int(time.time() - cid_timestamp)}s). Bot is cleanly wiped.")
                                            continue
                                except Exception as parse_e:
                                    logger.warning(f"Failed to parse timestamp from {cid}: {parse_e}")
                            
                            # Check if order is known and open
                            cursor.execute("SELECT status FROM bot_orders WHERE order_id=? OR client_order_id=?", (order['id'], cid))
                            row = cursor.fetchone()
                            
                            logger.info(f"🔍 [OFFLINE-SYNC] Checking Order {order['id']} (CID: {cid}) | InDB: {'Yes' if row else 'No'} | DBStatus: {row[0] if row else 'N/A'}")
                            
                            # If order is NOT in DB, or is 'open', we process it.
                            # If it is 'filled'/'closed'/'reset_cleared' already, skip.
                            if row and row[0] in ['filled', 'closed', 'reset_cleared']:
                                continue

                            # Update Bot State
                            order_status = order.get('status', '').lower()
                            # GUARD: Only process actually-filled orders. Cancelled/expired order
                            # history is also returned by fetch_closed_orders — skip them.
                            if order_status not in ('filled', 'closed'):
                                logger.debug(f"Skipping non-filled order {cid} (status={order_status})")
                                continue

                            fill_price = order.get('average') or order.get('price') or 0.0
                            fill_qty = order.get('filled') or order.get('amount') or 0.0
                            fill_symbol = order.get('symbol', pair)
                            bot_name = f"Bot-{bot_id}" # Placeholder, ideally fetch name
                            
                            curr_step = current_state.get('current_step', 0)
                            
                            logger.info(f"🕵️ RECONSTRUCTING: Found Fill for Bot {bot_id} {otype} {step} @ {fill_price} for {fill_symbol}")

                            if otype == 'TP':
                                # GUARD: Never reset with price=0 — that corrupts trade history
                                if fill_price <= 0:
                                    logger.warning(f"⚠️ Skipping OFFLINE_TP for Bot {bot_id}: fill_price={fill_price} is invalid.")
                                    continue
                                # Only process if we haven't reset yet?
                                # Ideally yes. If we are active, and find a TP fill, we MUST reset.
                                self._handle_offline_tp_fill(bot_id, bot_name, fill_price, fill_symbol)
                                stats['tp_fills'] += 1
                                
                            elif otype == 'GRID':
                                # MODIFIED: Allow step >= curr_step to recover from "Alzheimer's" repeat-fills
                                # If the order ID wasn't already marked closed/filled in DB, we MUST credit it.
                                if step >= curr_step:
                                    logger.info(f"✅ Re-playing OFFLINE_GRID for Bot {bot_id} (Step {step})")
                                    self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, fill_qty, curr_step, current_state.get('total_invested',0), current_state.get('avg_entry',0), fill_symbol)
                                    stats['grid_fills'] += 1
                                    # Update local state for subsequent orders in same history fetch
                                    bot_states[bot_id]['current_step'] = step
                                    bot_states[bot_id]['total_invested'] += (fill_price * fill_qty)
                                    # (avg_entry update omitted for local cache brevity, will be correct in DB)
                                    
                            elif otype == 'ENTRY':
                                if curr_step == 0:
                                    logger.info(f"✅ Re-playing OFFLINE_ENTRY for Bot {bot_id}")
                                    order_time_sec = int(order.get('timestamp', time.time() * 1000) / 1000)
                                    self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, fill_qty, fill_symbol, order_time_sec)
                                    stats['entry_fills'] += 1
                                    bot_states[bot_id]['current_step'] = 1
                                    bot_states[bot_id]['entry_confirmed'] = 1
                                    bot_states[bot_id]['total_invested'] = fill_price * fill_qty
                            
                            # Update/Insert Order Record
                            # bot_orders has no UNIQUE on order_id, so check first.
                            cursor.execute("SELECT id FROM bot_orders WHERE order_id=?", (order['id'],))
                            existing = cursor.fetchone()
                            if existing:
                                cursor.execute("UPDATE bot_orders SET status='filled', updated_at=? WHERE order_id=?",
                                               (int(time.time()), order['id']))
                            else:
                                cursor.execute("""
                                    INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, updated_at, client_order_id, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?)
                                """, (bot_id, step, otype.lower(), order['id'], fill_price, fill_qty,
                                      int(order['timestamp']/1000), int(time.time()), cid, 'Reconstructed from History'))
                                    
                        except Exception as e:
                            logger.debug(f"Error parsing/processing CID {cid}: {e}")
                            continue

                    conn.commit()
                    conn.close()

                except Exception as e:
                    logger.error(f"Error scanning pair {pair} on {mt}: {e}")

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
        new_step = (current_step or 0) + 1
        fill_cost = fill_price * fill_amount
        new_invested = (total_invested or 0) + fill_cost
        new_avg = ((total_invested or 0) * (avg_entry or 0) + fill_cost) / new_invested if new_invested > 0 else fill_price
        
        # UPSERT trade
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=? WHERE bot_id=?", 
                           (new_step, new_invested, new_avg, bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price) VALUES (?,?,?,?)",
                           (bot_id, new_step, new_invested, new_avg))
        
        log_trade(bot_id, 'OFFLINE_GRID', symbol, fill_price, fill_amount, fill_cost, f"GRID_{new_step}", new_step, "Offline Grid Fill", 0)

    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, symbol, timestamp_sec):
        fill_cost = fill_price * fill_amount
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=1, total_invested=?, avg_entry_price=?, entry_confirmed=1, basket_start_time=? WHERE bot_id=?",
                           (fill_cost, fill_price, timestamp_sec, bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time) VALUES (?,1,?,?,1,?)",
                           (bot_id, fill_cost, fill_price, timestamp_sec))
        
        log_trade(bot_id, 'OFFLINE_ENTRY', symbol, fill_price, fill_amount, fill_cost, "ENTRY", 1, "Offline Entry Fill", 0)

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
                    # --- PHANTOM BOT CORRECTION ---
                    # If invested > 0 but NO orders and NO physical position (checked in resolve_net_mismatch)
                    # For now, just log and let resolve_net_mismatch handle the structural lock.
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
            
        # FUNDAMENTAL FIX: Include pairs that have positions but no bots (Rogue Positions)
        # We must ensure that EVERY pair in positions is a key in bots_by_pair
        all_exchange_pairs = list(positions.keys())
        for p in all_exchange_pairs:
            norm_p = normalize_symbol(p)
            if norm_p not in bots_by_pair:
                bots_by_pair[norm_p] = [] # Empty list of bots for this pair

        for pair, bots in bots_by_pair.items():
            # 'pair' here is normalized
            # 1. Calc Virtual Net
            virtual_net = 0.0
            total_virtual_invested = 0.0
            virtual_net_usd = 0.0  # Signed USD value
            
            for b in bots:
                if b.in_trade:
                    qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    total_virtual_invested += b.total_invested
                    if b.direction.upper() == 'LONG':
                        virtual_net += qty
                        virtual_net_usd += b.total_invested
                    else:
                        virtual_net -= qty
                        virtual_net_usd -= b.total_invested
            
            # 2. Get Physical Net
            # 3. Compare with Tolerance
            # Ensure we are comparing normalized symbols
            pair_normalized = normalize_symbol(pair)
            pair_positions = positions.get(pair_normalized, [])
            
            total_physical_notional = 0.0
            physical_net = 0.0
            physical_net_usd = 0.0 # Signed USD value
            rep_side = "N/A"
            
            for p in pair_positions:
                val = p.size * p.entry_price
                total_physical_notional += val
                rep_side = p.side # Used for reporting
                if p.side == 'LONG': 
                     physical_net += p.size
                     physical_net_usd += val
                else: 
                     physical_net -= p.size
                     physical_net_usd -= val
                
            logger.info(f"⚖️ RECON AUDIT [{pair_normalized}]: Virtual=${total_virtual_invested:.2f} vs Physical=${total_physical_notional:.2f}")
            
            # Case A: Total System Investment > 10.0 but Physical = 0
            if total_virtual_invested > 10.0 and total_physical_notional < 1.0:
                logger.critical(f"👻 SYSTEM MISMATCH on {pair}: Virtual=${total_virtual_invested:.2f} vs Physical=$0.00.")
                
                # --- FUNDAMENTAL ARCHITECTURE: EVIDENCE-BASED RECONSTRUCTION ---
                for b in bots:
                    if b.in_trade:
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
                            # --- FUNDAMENTAL FIX: VERIFY ENTRY EXISTENCE ---
                            # If no exit proof, maybe the entry itself never happened (Phantom Entry)?
                            
                            # SKIP IF AUDITED/MANUAL
                            # If we manually confirmed it, don't delete it just because the order is old/missing.
                            if b.has_confirmed_entry:
                                logger.info(f"🛡️ SKIPPING Entry Verification for Bot {b.name}: Entry already confirmed (Manual/Audit).")
                                continue

                            entry_proof = self._verify_entry_existence(b)
                            if not entry_proof:
                                logger.info(f"✅ INVALID ENTRY DETECTED: Bot {b.name} Entry Order {b.entry_order_id} not found/filled on exchange. Resetting Phantom State.")
                                from .database import log_reconciliation
                                log_reconciliation(
                                    bot_id=b.bot_id,
                                    pair=b.pair,
                                    action="RESET_PHANTOM_ENTRY",
                                    details="Entry order not found or not filled on exchange. Bot state was phantom.",
                                )
                                res = ReconciliationResult(
                                    bot_id=b.bot_id,
                                    bot_name=b.name,
                                    pair=b.pair,
                                    action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                    details=f"Reset Phantom Entry (Order {b.entry_order_id} invalid)",
                                    requires_manual_intervention=False
                                )
                                results.append(res)
                                self._fix_ghost_bot(b, proof_order_id="PHANTOM_ENTRY")
                                # FORCE RESET: If the exchange says $0, the position is gone (e.g. user manually closed it on exchange).
                                # We MUST adopt reality rather than stalling the bot indefinitely.
                                logger.critical(
                                    f"💥 [MANUAL-INTERVENTION DETECTED] Bot {b.name}: Virtual claims ${b.total_invested:.2f} but Physical is $0.00! "
                                    f"The position vanished without bot logic (user flattened). Forcing memory wipe to sync with reality!"
                                )
                                from .database import reset_bot_after_tp, log_reconciliation
                                reset_bot_after_tp(b.bot_id, 0.0) # Reset to 0 since it vanished
                                
                                log_reconciliation(
                                    bot_id=b.bot_id,
                                    pair=b.pair,
                                    action="RESET_VANISHED_POSITION",
                                    details="Mismatch: Virtual>0, Physical=0. Position vanished (likely user closed). Resetting bot."
                                )
                                results.append(ReconciliationResult(
                                    bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                    action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                    details="Reset Vanished Position", requires_manual_intervention=False
                                ))
            
            # --- CASE B.2: STRUCTURAL GHOST DETECTION ---
            # Even if net-notional is close, if a bot claims high step but lacks history, it's a structural ghost (legacy state).
            for b in bots:
                if b.in_trade and b.total_invested > 1.0:
                    try:
                        from .database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT COUNT(*), SUM(amount * price) FROM bot_orders 
                            WHERE bot_id=? AND status='filled' AND created_at >= (? - 120)
                        """, (b.bot_id, b.basket_start_time))
                        row = cursor.fetchone()
                        
                        # If bot has money invested, but ZERO filled orders in the current basket session
                        if not row or row[0] == 0:
                            logger.critical(f"👻 [STRUCTURAL-GHOST] Bot {b.name} claims ${b.total_invested:.2f} (Step {b.current_step}) but has NO filled orders since basket start ({b.basket_start_time}). Resetting to truth.")
                            from .database import reset_bot_after_tp, log_reconciliation
                            reset_bot_after_tp(b.bot_id, 0.0) # Force reset
                            log_reconciliation(
                                bot_id=b.bot_id, pair=b.pair, action="RESET_STRUCTURAL_GHOST",
                                details=f"Structural Ghost: Claimed ${b.total_invested:.2f} but 0 Session Fills."
                            )
                            results.append(ReconciliationResult(
                                bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details="Reset Structural Ghost (No verifiable history)", requires_manual_intervention=False
                            ))
                    except Exception as e:
                        logger.error(f"Error checking structural ghost for {b.name}: {e}")
                    finally:
                        if 'conn' in locals(): conn.close()

            # --- CASE C: SIGNIFICANT NOTIONAL DEVIATION (AUTO-REPAIR) ---
            # Both exist, but delta is massive — attempt to reconcile using local history
            delta_notional = abs(total_virtual_invested - total_physical_notional)
            if total_virtual_invested > 10.0 and total_physical_notional > 1.0 and delta_notional > 50.0:
                 logger.warning(
                     f"⚠️ [NOTIONAL-GAP] {pair}: Virtual=${total_virtual_invested:.2f} vs "
                     f"Physical=${total_physical_notional:.2f} (Delta=${delta_notional:.2f}). Attempting auto-repair..."
                 )
                 
                 # 🚀 AUTO-REPAIR: If Physical > Virtual, it's likely missed fills (Memory Gap)
                 if total_physical_notional > total_virtual_invested:
                     for target_bot in [b for b in bots if b.in_trade]:
                         try:
                             from .database import get_connection, accumulate_trade_fill
                             conn = get_connection()
                             cursor = conn.cursor()
                             
                             # Check if bot_orders has more filled value than we have in trades
                             # Filter by basket_start_time to ensure we only sum CURRENT session fills
                             cursor.execute("""
                                 SELECT SUM(amount * price), SUM(amount), MAX(step) 
                                 FROM bot_orders 
                                 WHERE bot_id = ? AND status = 'filled'
                                 AND created_at >= (? - 120)
                             """, (target_bot.bot_id, target_bot.basket_start_time))
                             row = cursor.fetchone()
                             if row and row[0] and row[0] > (target_bot.total_invested + 1.0):
                                 history_sum = row[0]
                                 history_qty = row[1]
                                 history_step = row[2] or 0
                                 
                                 repair_amount = history_sum - target_bot.total_invested
                                 current_qty = (target_bot.total_invested / target_bot.avg_entry_price) if target_bot.avg_entry_price > 0 else 0
                                 repair_qty = history_qty - current_qty
                                 
                                 logger.critical(f"👨‍⚕️ [RECON-REPAIR] Bot {target_bot.name} (ID {target_bot.bot_id}) is missing ${repair_amount:,.2f} from trade balance. REPAIRING from current session history.")
                                 
                                 avg_price = history_sum / history_qty if history_qty > 0 else target_bot.avg_entry_price
                                 
                                 # Apply the missing portion
                                 accumulate_trade_fill(target_bot.bot_id, repair_amount, repair_qty, avg_price, max(history_step, target_bot.current_step), 0.0)
                                 
                                 results.append(ReconciliationResult(
                                     bot_id=target_bot.bot_id,
                                     bot_name=target_bot.name,
                                     pair=target_bot.pair,
                                     action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                     details=f"Repaired: ${repair_amount:,.2f} cost basis recovered from order history.",
                                     requires_manual_intervention=False
                                 ))
                                 # Removed break to allow multi-repair
                         except Exception as repair_err:
                             logger.error(f"❌ Auto-repair failed for Bot {target_bot.bot_id}: {repair_err}")
                         finally:
                             if 'conn' in locals(): conn.close()

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
            
            # 2. Identify Direction of Error
            if abs(net_error) > 50.0:
                error_side = 'LONG' if net_error > 0 else 'SHORT'
                logger.warning(
                    f"⚠️ [NET-MISMATCH] {pair_normalized}: Virtual Net=${virt_net:.2f}, Physical Net=${phys_net:.2f}. "
                    f"Diff=${net_error:.2f} (Virtual is too {error_side}). Checking for ghosts..."
                )
                
                # 3. Identify Suspects (Bots contributing to the error)
                suspects = [b for b in bots if b.in_trade and b.direction.upper() == error_side]
                
                # Get orders for this pair
                pair_orders = all_orders.get(pair_normalized, [])
                
                for b in suspects:
                    # 4. Proof of Life Check: Does this bot have open orders?
                    bot_orders = [o for o in pair_orders if o.client_order_id and f"CQB_{b.bot_id}_" in o.client_order_id]
                    
                    if not bot_orders:
                        # FLAG ONLY — do not reset. Bot may be between order placement cycles.
                        logger.warning(
                            f"⚠️ [NET-SUM-FLAG] Bot {b.name} (ID {b.bot_id}) is {b.direction} "
                            f"${b.total_invested:.2f} with no open orders. Contributes to net error. "
                            f"Flagging only — NO RESET."
                        )
                    else:
                         logger.info(f"🛡️ [GHOST-SAFE] Bot {b.name} has {len(bot_orders)} open orders.")

            # Case B: Physical exists but System says 0 — AUTO-HEALING RECOVERY
            if total_physical_notional > 50.0 and total_virtual_invested < 1.0:
                 logger.warning(f"⚠️ [MEMORY-GAP] {pair}: Physical=${total_physical_notional:.2f} exists but Sys=0. Searching Order History for orphan fills...")
                 
                 # 1. Identify active candidate bots
                 candidate_bots = [b for b in bots if b.is_active]
                 
                 found_recovery = False
                 if candidate_bots:
                     for target_bot in candidate_bots:
                         try:
                             from .database import get_connection, accumulate_trade_fill
                             conn = get_connection()
                             cursor = conn.cursor()
                             
                             # Sum all filled orders for THIS bot session (or any time if trade table is empty)
                             # We use history to explain the gap, but only from current basket.
                             cursor.execute("""
                                 SELECT SUM(amount * price), SUM(amount), MAX(step) 
                                 FROM bot_orders 
                                 WHERE bot_id = ? AND status = 'filled'
                                 AND created_at >= (? - 120)
                             """, (target_bot.bot_id, target_bot.basket_start_time))
                             row = cursor.fetchone()
                             if row and row[0] and row[0] > 10.0:
                                 history_sum = row[0]
                                 history_qty = row[1]
                                 history_step = row[2] or 0
                                 avg_price = history_sum / history_qty if history_qty > 0 else 0.0
                                 
                                 logger.critical(f"🧟 [RECON-RECOVERY] Bot {target_bot.name} (ID {target_bot.bot_id}) has ${history_sum:,.2f} in orphan fills. ADOPTING position basis.")
                                 
                                 # Reconstruct state
                                 accumulate_trade_fill(target_bot.bot_id, history_sum, history_qty, avg_price, history_step, 0.0)
                                 
                                 results.append(ReconciliationResult(
                                     bot_id=target_bot.bot_id,
                                     bot_name=target_bot.name,
                                     pair=target_bot.pair,
                                     action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                     details=f"Reconstructed: ${history_sum:,.2f} from basket history.",
                                     requires_manual_intervention=False
                                 ))
                                 found_recovery = True
                                 # Removed break to allow multi-repair
                         except Exception as rec_err:
                             logger.error(f"❌ Recovery failed for Bot {target_bot.bot_id}: {rec_err}")
                         finally:
                             if 'conn' in locals(): conn.close()
 
                 if not found_recovery:
                     # 🚀 REPORT ROGUE POSITION (for UI Wizard)
                     res = ReconciliationResult(
                         bot_id=0,
                         bot_name="EXCHANGE_ONLY",
                         pair=pair,
                         action_taken=ReconciliationAction.ROGUE_POSITION,
                         details=f"Exchange has ${total_physical_notional:.2f} ({rep_side}) but no bot claims it.",
                         requires_manual_intervention=True
                     )
                     results.append(res)
 
                     logger.warning(
                         f"⚠️ UNMATCHED POSITION on {pair}: Physical=${total_physical_notional:.2f} "
                         f"exists but no bot claims it. No local history found to explain it."
                     )
                     from .database import log_reconciliation
                     log_reconciliation(
                         bot_id=0,
                         pair=pair,
                         action="ROGUE_POSITION",
                         details=f"Exchange has ${total_physical_notional:.2f} but no bot claims it."
                     )
        
        return results

    def _find_proof_of_exit(self, bot: BotState) -> Optional[Dict]:
        """
        Searches exchange history for proof that the bot's position was closed.
        Uses the clientOrderId DNA (CQB_BOTID_TP/SL/...)
        """
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                # Fetch history for the specific pair
                history = ex.fetch_closed_orders(bot.pair, limit=50)
                if not isinstance(history, list):
                    continue
                
                for order in history:
                    cid = order.get('clientOrderId', '')
                    # We look for any exit order from this bot
                    if cid.startswith(f"CQB_{bot.bot_id}_TP_") or cid.startswith(f"CQB_{bot.bot_id}_SL_"):
                        if order.get('status') in ['closed', 'filled']:
                            return order
            except Exception as e:
                logger.error(f"Failed to fetch exit proof for Bot {bot.bot_id}: {e}")
        return None

    def _verify_entry_existence(self, bot: BotState) -> bool:
        """
        Verifies if the bot's supposed Entry Order actually exists and is filled.
        Returns True if valid entry found, False if missing/cancelled (Phantom).
        """
        if not bot.entry_order_id:
            return False # No Order ID = Phantom State

        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                # remove prefix if needed, though fetch_order handles ID usually
                # We need to use the exchange interface wrapper
                order = ex.fetch_order(bot.entry_order_id, bot.pair)
                if order:
                    status = order.get('status', '').lower()
                    if status == 'filled':
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
        
        # 1. Offline Fills (Updates DB)
        self.detect_offline_fills()
        
        # 2. Fetch Fresh State
        bot_states = self.get_bot_states()
        success, all_positions = self.fetch_all_exchange_positions()
        
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
                    entry_confirmed=0, basket_start_time=0
                WHERE bot_id=?
            """, (bot_id,))
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
                action='GHOST_RESET',
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
                SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=0,
                    avg_entry_price=0, target_tp_price=0
                WHERE bot_id=?
            """, (bot.bot_id,))
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot.bot_id,))
            
            # Also cancel any open internal orders to prevent zombie revival
            cursor.execute("UPDATE bot_orders SET status='cancelled' WHERE bot_id=? AND status='open'", (bot.bot_id,))
            
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
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                raw = ex.fetch_positions()
                if not isinstance(raw, list):
                    logger.warning(f"⚠️ fetch_positions returned non-list for exchange '{mt}': {raw}. Skipping.")
                    continue
                for p in raw:
                    sym = normalize_symbol(p.get('symbol', ''))
                    pos = ExchangePosition(
                        symbol=sym,
                        side='LONG' if float(p.get('contracts',0) or p.get('size',0)) > 0 else 'SHORT',
                        size=abs(float(p.get('contracts',0) or p.get('size',0))),
                        entry_price=float(p.get('entryPrice',0)),
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
        for pair in pairs:
            orders_by_pair[pair] = []
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    raw = ex.fetch_open_orders(pair)
                    if not isinstance(raw, list):
                        logger.warning(f"⚠️ fetch_open_orders returned non-list for exchange '{mt}', pair '{pair}': {raw}. Skipping.")
                        continue
                    for o in raw:
                        orders_by_pair[pair].append(ExchangeOrder(
                            order_id=str(o.get('id','')),
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
            
            status = get_bot_status(bot_id)
            if not status: 
                logger.warning(f"⚠️ [GET-STATE] No status found for Bot {bot_id}")
                continue
            
            # Use raw DB confirmed status if needed
            order_ids = get_bot_order_ids(bot_id)
            cursor.execute("SELECT COUNT(*) FROM trade_history WHERE bot_id=? AND action IN ('BUY','SELL') AND timestamp > ?", (bot_id, int(time.time()-86400)))
            confirmed = cursor.fetchone()[0] > 0
            
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
                has_confirmed_entry=confirmed
            ))
        conn.close()
        return states

# Alias for backward compatibility if needed
DeepReconciler = StateReconciler
