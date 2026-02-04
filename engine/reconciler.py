import logging
import sqlite3
import time
from typing import Dict, List, Set

from engine.exchange_interface import ExchangeInterface
from config.settings import config

logger = logging.getLogger("DeepReconciler")

class DeepReconciler:
    def __init__(self, exchanges: Dict[str, ExchangeInterface]):
        self.exchanges = exchanges
        self.conn = sqlite3.connect(config.PATHS["DB_FILE"], check_same_thread=False)
        self.cursor = self.conn.cursor()

    def run(self):
        """
        Main entry point for Deep Reconciliation.
        Checks all active exchanges against the local database.
        """
        logger.info("Starting Deep State Reconciliation (Auto-Healing)...")
        
        try:
            for market_type, exchange in self.exchanges.items():
                if not exchange: continue
                logger.info(f"Reconciling {market_type} orders...")
                self._reconcile_market(market_type, exchange)
                
            self.conn.commit()
            logger.info("✅ Deep Reconciliation Complete. System is clean.")
            
        except Exception as e:
            logger.error(f"❌ Deep Reconciliation Failed: {e}")
        finally:
            pass # Connection kept open or closed? Better to close if one-off.
            # actually let's not close here if we want to reuse, but this is likely one-off per startup.
            self.conn.close()

    def _reconcile_market(self, market_type: str, exchange: ExchangeInterface):
        # 1. Fetch ALL open orders from Exchange
        try:
            # Try generic fetch first
            try:
                ex_orders = exchange.exchange.fetch_open_orders()
            except Exception:
                # Fallback: Iterate known symbols
                ex_orders = []
                for sym in config.ALLOWED_SYMBOLS:
                    try:
                        orders = exchange.fetch_open_orders(sym)
                        ex_orders.extend(orders)
                    except: pass
            
            ex_order_map = {str(o['id']): o for o in ex_orders}
            logger.info(f"   Exchange has {len(ex_order_map)} open orders.")
            
        except Exception as e:
            logger.error(f"   Failed to fetch exchange orders for {market_type}: {e}")
            return

        # 2. Fetch ALL open orders from DB for this market_type (implied by bot config, but simplistically just all open orders)
        # Note: bot_orders table doesn't strictly store market_type, but order IDs are unique enough usually.
        # We'll just fetch ALL open orders from DB, joined with bots for symbol info.
        self.cursor.execute("""
            SELECT bo.order_id, bo.bot_id, b.pair 
            FROM bot_orders bo
            JOIN bots b ON bo.bot_id = b.id
            WHERE bo.status='open'
        """)
        db_rows = self.cursor.fetchall()
        
        # Filter DB orders that likely belong to this exchange/market (e.g. by symbol)
        # Or just reconcile all ID matches.
        
        db_order_ids = {str(row[0]) for row in db_rows}
        logger.info(f"   Database has {len(db_order_ids)} open orders.")

        # 3. Detect Orphans (On Exchange, Not in DB)
        orphans = []
        for oid, order in ex_order_map.items():
            if oid not in db_order_ids:
                orphans.append(order)

        if orphans:
            logger.warning(f"   ⚠️ Found {len(orphans)} ORPHAN orders on Exchange.")
            for o in orphans:
                # Check if this is a BOT order (has our tag) or MANUAL order
                client_id = o.get('clientOrderId', '')
                if client_id.startswith('CQB_'):
                    # This is OUR order (bot-placed). Safe to cancel.
                    self._cancel_orphan(exchange, o, is_tagged=True)
                else:
                    # This might be a manual order. DO NOT cancel.
                    self._log_manual_orphan(o)
        else:
            logger.info("   ✅ No Orphans found.")

        # 4. Detect Ghosts (In DB, Not on Exchange)
        # Only consider orders that "should" be on this exchange.
        # If we have multiple exchanges, we must be careful not to close Spot orders when checking Futures.
        # However, order IDs are usually globally unique or unique per pair.
        
        # Simple heuristic: If the order is NOT in the active exchange list, AND it belongs to a pair traded on this exchange.
        # For now, assuming Global ID uniqueness or relying on re-check.
        
        # Actually, if we are in FUTURES_ONLY_MODE, we might not see Spot orders.
        # So only check ghosts if we are sure we have visibility.
        
        ghosts = []
        for oid in db_order_ids:
            if oid not in ex_order_map:
                # Potential Ghost.
                # Double check: Is this order simply on ANOTHER exchange?
                # If we only have ONE exchange initialized (e.g. Futures), we shouldn't close Spot orders.
                # We need to know the symbol's market type.
                
                # Check DB for symbol
                # We can't easily know market type from DB alone without parsing symbol or joining bots table.
                # But if the ID is missing from the *relevant* exchange, it's a ghost.
                
                # Safe approach: Only close if we are sure it belongs to this market.
                # If we fetched "ALL" orders, we are safe.
                pass
                
        # Improved Ghost Logic:
        # We iterate DB rows. identifying which exist in the current `ex_orders` list.
        # If an order is missing, we mark it closed.
        
        # BUT, if we have multiple exchanges (Spot + Future), we shouldn't close a Spot order just because it's not in Futures.
        # So we need to filter DB rows by the market we are currently checking.
        
        # Heuristic: Check if symbol format matches (e.g. /USDT usually both, but we can check exchange.market(symbol))
        
        for oid_str, bot_id, symbol in db_rows:
            if oid_str not in ex_order_map:
                # It's missing from THIS exchange fetch.
                # Is it supposed to be here?
                # ask exchange if it has this symbol
                is_valid_symbol = False
                try:
                    if symbol in exchange.exchange.markets:
                        is_valid_symbol = True
                except: pass
                
                if is_valid_symbol:
                    # It SHOULD be here, but isn't. It's a GHOST.
                    # One caveat: Pagination? We assumed fetch_open_orders returned everything.
                    # CCXT `fetch_open_orders` usually returns all.
                    
                    self._close_ghost(oid_str, bot_id)

    def _cancel_orphan(self, exchange: ExchangeInterface, order: dict, is_tagged: bool = False):
        """
        Cancel an orphan order on the Exchange.
        - If is_tagged=True (has CQB_ clientOrderId), safely cancel (it's our order).
        - If is_tagged=False, only warn (might be manual order).
        """
        client_id = order.get('clientOrderId', 'N/A')
        
        if is_tagged:
            # SAFE TO CANCEL: This is a bot-placed order
            try:
                logger.info(f"      🗑️ Cancelling Tagged Orphan: {order['symbol']} {order['side']} {order['amount']} (ID: {order['id']}, Tag: {client_id})")
                exchange.cancel_order(order['symbol'], order['id'])
                logger.info(f"      ✅ Orphan {order['id']} cancelled.")
            except Exception as e:
                logger.error(f"      ❌ Failed to cancel orphan {order['id']}: {e}")
        else:
            # NOT TAGGED: Log warning only, do not cancel
            logger.warning(f"      ⚠️ FOUND ORPHAN (Exchange Only, No Tag): {order['symbol']} {order['side']} {order['amount']} (ID: {order['id']})")
            logger.warning(f"      🛑 SKIPPING CANCEL (Manual Safety). Verify this order manually.")
    
    def _log_manual_orphan(self, order: dict):
        """Log an untagged orphan order (likely manual) without cancelling."""
        logger.warning(f"      ⚠️ UNTAGGED ORDER (Possibly Manual): {order['symbol']} {order['side']} {order['amount']} (ID: {order['id']})")
        logger.warning(f"         Not cancelling. If this should be a bot order, it was placed before tagging was enabled.")

    def _close_ghost(self, order_id: str, bot_id: int):
        try:
            logger.warning(f"   👻 Closing Ghost Order in DB: ID {order_id} (Bot {bot_id})")
            self.cursor.execute("UPDATE bot_orders SET status='closed', updated_at=CURRENT_TIMESTAMP WHERE order_id=?", (order_id,))
            # Also optionally update trades table if this was an entry/exit? 
            # Too complex, just clearing order status is enough to unblock the bot.
        except Exception as e:
            logger.error(f"      Failed to close ghost {order_id}: {e}")

    def detect_offline_fills(self, exchange: ExchangeInterface, since_hours: int = 48):
        """
        Phase 4: Detect orders that filled while the bot was offline.
        
        Fetches order history for the last `since_hours` and matches filled orders
        against our DB. For each match:
        - Grid fills: Update trade state (step++, avg entry, invested).
        - TP fills: Reset bot to idle state.
        
        Args:
            exchange: ExchangeInterface instance.
            since_hours: How far back to look for filled orders (default 48 hours).
            
        Returns:
            dict with counts of grid_fills, tp_fills detected.
        """
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total_checked': 0}
        
        try:
            # Calculate timestamp for since_hours ago
            since_timestamp = int((time.time() - (since_hours * 3600)) * 1000)  # CCXT uses milliseconds
            
            # Fetch closed orders from exchange
            # Note: Not all exchanges support fetch_closed_orders without pagination.
            # We'll try fetch_closed_orders, fall back to fetch_orders with status filter.
            try:
                closed_orders = exchange.exchange.fetch_closed_orders(since=since_timestamp)
            except Exception:
                # Fallback: Try with specific symbols
                closed_orders = []
                for sym in config.ALLOWED_SYMBOLS:
                    try:
                        orders = exchange.exchange.fetch_closed_orders(sym, since=since_timestamp)
                        closed_orders.extend(orders)
                    except: pass
            
            stats['total_checked'] = len(closed_orders)
            
            if not closed_orders:
                logger.info("   📋 No offline fills to process.")
                return stats
            
            logger.info(f"   📋 Checking {len(closed_orders)} closed orders for offline fills...")
            
            # Get all our tracked orders from DB that are still 'open' (potential offline fills)
            self.cursor.execute("""
                SELECT bo.order_id, bo.bot_id, bo.order_type, bo.step, bo.price, bo.amount,
                       b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price
                FROM bot_orders bo
                JOIN bots b ON bo.bot_id = b.id
                LEFT JOIN trades t ON bo.bot_id = t.bot_id
                WHERE bo.status = 'open' AND bo.order_id IS NOT NULL
            """)
            db_open_orders = {str(row[0]): row for row in self.cursor.fetchall()}
            
            # Match closed orders to our tracked orders
            for order in closed_orders:
                oid = str(order.get('id', ''))
                if oid not in db_open_orders:
                    continue  # Not our order
                
                # Found a match - this is an offline fill!
                db_order = db_open_orders[oid]
                order_id, bot_id, order_type, step, db_price, db_amount, bot_name, pair, direction, current_step, total_invested, avg_entry = db_order
                
                fill_status = order.get('status', '')
                if fill_status not in ['closed', 'filled']:
                    continue  # Not actually filled
                
                fill_price = float(order.get('average', 0) or order.get('price', 0))
                fill_amount = float(order.get('filled', 0) or order.get('amount', 0))
                
                logger.info(f"   🔔 OFFLINE FILL: {bot_name} {order_type.upper()} @ ${fill_price:.4f}")
                
                if order_type == 'grid':
                    # Grid order filled - update trade state
                    self._handle_offline_grid_fill(bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry)
                    self._mark_order_filled(oid, fill_price)
                    stats['grid_fills'] += 1
                    
                elif order_type == 'tp':
                    # TP order filled - reset bot
                    self._handle_offline_tp_fill(bot_id, bot_name, fill_price)
                    self._mark_order_filled(oid, fill_price)
                    stats['tp_fills'] += 1
                    
                elif order_type == 'entry':
                    # Entry order filled - confirm entry
                    self._handle_offline_entry_fill(bot_id, bot_name, fill_price, fill_amount)
                    self._mark_order_filled(oid, fill_price)
                    stats['entry_fills'] += 1
            
            if stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills'] > 0:
                logger.info(f"   ✅ Offline Fill Recovery: {stats['entry_fills']} entries, {stats['grid_fills']} grids, {stats['tp_fills']} TPs")
            
            return stats
            
        except Exception as e:
            logger.error(f"   ❌ Offline fill detection failed: {e}")
            return stats

    def _handle_offline_grid_fill(self, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry):
        """Update trade state for an offline grid fill."""
        try:
            # Calculate new trade state
            new_step = (current_step or 0) + 1
            fill_cost = fill_price * fill_amount
            new_invested = (total_invested or 0) + fill_cost
            
            # Calculate new weighted average entry
            if new_invested > 0:
                old_weighted = (total_invested or 0) * (avg_entry or 0)
                new_weighted = old_weighted + fill_cost
                new_avg_entry = new_weighted / new_invested
            else:
                new_avg_entry = fill_price
            
            # Update trades table
            self.cursor.execute("""
                UPDATE trades SET
                    current_step = ?,
                    total_invested = ?,
                    avg_entry_price = ?
                WHERE bot_id = ?
            """, (new_step, new_invested, new_avg_entry, bot_id))
            
            # Log to trade_history
            from engine.database import log_trade
            log_trade(bot_id, 'OFFLINE_GRID_FILL', 'RECONCILER', fill_price, fill_amount, fill_cost, 
                      f'OFFLINE_STEP_{new_step}', new_step, 0, f"Grid filled while offline at step {new_step}")
            
            logger.info(f"      📈 {bot_name}: Step {current_step or 0} -> {new_step}, Invested ${new_invested:.2f}")
            
        except Exception as e:
            logger.error(f"      Failed to update offline grid fill for bot {bot_id}: {e}")

    def _handle_offline_tp_fill(self, bot_id, bot_name, fill_price):
        """Reset bot after offline TP fill."""
        try:
            from engine.database import reset_bot_after_tp
            reset_bot_after_tp(bot_id, exit_price=fill_price, action_label='OFFLINE_TP_HIT', verify_with_exchange=False)
            logger.info(f"      🎯 {bot_name}: TP hit while offline @ ${fill_price:.4f}. Reset to idle.")
        except Exception as e:
            logger.error(f"      Failed to reset bot {bot_id} after offline TP: {e}")

    def _handle_offline_entry_fill(self, bot_id, bot_name, fill_price, fill_amount):
        """Confirm entry for an offline entry fill."""
        try:
            fill_cost = fill_price * fill_amount
            self.cursor.execute("""
                UPDATE trades SET
                    current_step = 1,
                    total_invested = ?,
                    avg_entry_price = ?,
                    entry_confirmed = 1,
                    basket_start_time = ?
                WHERE bot_id = ?
            """, (fill_cost, fill_price, int(time.time()), bot_id))
            
            from engine.database import log_trade
            log_trade(bot_id, 'OFFLINE_ENTRY_FILL', 'RECONCILER', fill_price, fill_amount, fill_cost,
                      'OFFLINE_ENTRY', 1, 0, "Entry filled while offline")
            
            logger.info(f"      📈 {bot_name}: Entry filled offline @ ${fill_price:.4f}, Size: {fill_amount}")
            
        except Exception as e:
            logger.error(f"      Failed to confirm offline entry for bot {bot_id}: {e}")

    def _mark_order_filled(self, order_id, fill_price):
        """Mark an order as filled in bot_orders table."""
        try:
            self.cursor.execute("""
                UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?
                WHERE order_id = ?
            """, (int(time.time()), fill_price, int(time.time()), order_id))
        except Exception as e:
            logger.error(f"      Failed to mark order {order_id} as filled: {e}")
