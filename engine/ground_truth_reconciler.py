import time
import logging
from typing import Dict, Any
from config.settings import config

logger = logging.getLogger("GroundTruthReconciler")

class GroundTruthReconciler:
    """
    INV-31: Continuous physical-vs-virtual reconciliation.
    
    Runs every N cycles. For each active bot:
    1. Reads virtual state from trades table
    2. Reads physical state from exchange positions
    3. Detects divergence
    4. Routes to correct self-healing action
    
    This is the safety net under all other invariants.
    """

    DIVERGENCE_CATEGORIES = ('GHOST_VIRTUAL', 'ORPHAN_PHYSICAL', 
                              'STUCK_CASCADE', 'HEDGE_DRIFT', 'IN_SYNC')

    CASCADE_TIMEOUT = 300  # seconds before a stuck cascade is re-triggered
    CYCLE_INTERVAL = 10    # run every N engine cycles

    def run(self, exchange, conn) -> Dict[str, Any]:
        """
        Execute one ground truth reconciliation pass.
        Returns summary dict for logging.
        """
        results = {
            'ghost_virtual': [],
            'orphan_physical': [],
            'stuck_cascade': [],
            'stuck_pending_cleared': [],
            'manual_proof': [],
            'hedge_drift': [],
            'in_sync_count': 0,
            'errors': []
        }

        # ── Step 1: Fetch physical positions from exchange directly ──────────
        positions = None
        try:
            positions = exchange.fetch_positions()
        except Exception as e:
            logger.error(f"[GTR] fetch_positions failed: {e}. Skipping pass.")
            results['errors'].append(str(e))
            return results

        # Build physical_net: {normalized_pair: signed_net_qty}
        physical_net = self._build_physical_net(positions)

        # ── Step 2: Fetch all active bots and their virtual state ───────────
        bots = conn.execute("""
            SELECT b.id, b.name, b.normalized_pair, b.direction, b.status,
                   b.bot_type, b.parent_bot_id,
                   t.open_qty, t.total_invested, t.cycle_id,
                   t.basket_start_time, b.cascade_started_at
            FROM bots b JOIN trades t ON t.bot_id = b.id
            WHERE b.is_active = 1
        """).fetchall()

        # ── Step 3: Run STUCK_CASCADE checks (per-bot) ───────────────────────
        stuck_statuses = ('pending_close', 'pending_hedge_close', 'FLATTENING',
                          'pending_flatten')
        non_stuck_bots = []
        for bot in bots:
            (bot_id, name, norm_pair, direction, status, bot_type,
             parent_id, open_qty, total_invested, cycle_id, basket_ts, cascade_started_at) = bot

            if status in stuck_statuses:
                # Use cascade_started_at if populated, fallback to basket_ts if not
                start_time = cascade_started_at if (cascade_started_at and cascade_started_at > 0) else basket_ts
                stuck_duration = int(time.time()) - int(start_time or 0)
                if stuck_duration > self.CASCADE_TIMEOUT:
                    results['stuck_cascade'].append(bot_id)
                    self._heal_stuck_cascade(bot_id, name, status, float(open_qty or 0.0),
                                             norm_pair, exchange, conn)

            # Check for stuck pending_placement orders
            stuck_pending = conn.execute("""
                SELECT bo.id, bo.client_order_id, bo.order_type, bo.created_at, b.pair
                FROM bot_orders bo JOIN bots b ON b.id = bo.bot_id
                WHERE bo.bot_id=? AND bo.status='pending_placement'
                AND bo.created_at < ?
            """, (bot_id, int(time.time()) - 30)).fetchall()
            
            for row in stuck_pending:
                try:
                    exchange.fetch_order(row[1], row[4])
                    # Found on exchange — update status, don't cancel
                except Exception as e:
                    err_str = str(e).lower()
                    if 'not found' in err_str or 'orderNotFound' in err_str or '-2013' in err_str or 'order does not exist' in err_str:
                        conn.execute(
                            "UPDATE bot_orders SET status='cancelled', "
                            "notes='GTR: pending_placement not found on exchange', "
                            "updated_at=? WHERE id=?",
                            (int(time.time()), row[0])
                        )
                        conn.commit()
                        results['stuck_pending_cleared'].append(bot_id)

            if status in stuck_statuses:
                pass
            elif status == 'REQUIRE_MANUAL_PROOF':
                if cascade_started_at > 0:
                    age = int(time.time()) - cascade_started_at
                else:
                    # cascade_started_at not set — use basket_start_time as proxy
                    age = int(time.time()) - int(basket_ts or 0)
                
                if age > 3600:  # 1 hour
                    # Compute USD value of discrepancy for operator alert
                    try:
                        pair_row = conn.execute(
                            "SELECT pair FROM bots WHERE id=?", (bot_id,)
                        ).fetchone()
                        if pair_row:
                            last_price = exchange.get_last_price(pair_row[0])
                            usd_value = abs(open_qty * (last_price or 0.0))
                        else:
                            usd_value = 0.0
                    except Exception:
                        usd_value = 0.0
                    
                    logger.critical(
                        f"[GTR-INV31] REQUIRE_MANUAL_PROOF: Bot {name} ({bot_id}) "
                        f"has been locked for {age//3600}h {(age%3600)//60}m. "
                        f"Virtual open_qty={open_qty:.6f} "
                        f"(~${usd_value:.2f} USD unresolved). "
                        f"Human intervention required — check CODEBASE_GUIDE §2 "
                        f"for resolution procedure."
                    )
                results['manual_proof'].append(bot_id)
                continue  # Do not attempt auto-heal on REQUIRE_MANUAL_PROOF
            else:
                non_stuck_bots.append(bot)

        # ── Step 4: Build virtual_net at PAIR level ─────────────────────────
        virtual_net = {}
        for bot in non_stuck_bots:
            (bot_id, name, norm_pair, direction, status, bot_type,
             parent_id, open_qty, total_invested, cycle_id, basket_ts, cascade_started_at) = bot
            
            qty = float(open_qty or 0.0)
            if qty > 0:
                is_long = direction.upper() == 'LONG'
                contribution = qty if is_long else -qty
                virtual_net[norm_pair] = virtual_net.get(norm_pair, 0.0) + contribution

        # ── Step 5: Compare at PAIR level ───────────────────────────────────
        all_pairs = set(physical_net.keys()).union(virtual_net.keys())
        for pair in all_pairs:
            phys = physical_net.get(pair, 0.0)
            virt = virtual_net.get(pair, 0.0)
            drift = phys - virt

            # Get step_size tolerance for this pair
            tolerance = self._get_tolerance_for_pair(pair, non_stuck_bots, conn, exchange)

            if abs(drift) <= tolerance:
                results['in_sync_count'] += 1
                continue

            # PAIR_GHOST_VIRTUAL: virtual says we hold, exchange says flat
            if abs(virt) > tolerance and abs(phys) <= tolerance:
                # Find all bots with open_qty > 0 on this pair and check for open orders
                for bot in non_stuck_bots:
                    (bot_id, name, norm_pair, direction, status, bot_type,
                     parent_id, open_qty, total_invested, cycle_id, basket_ts, cascade_started_at) = bot
                    
                    if norm_pair == pair and float(open_qty or 0.0) > tolerance:
                        open_order_count = conn.execute(
                            "SELECT COUNT(*) FROM bot_orders "
                            "WHERE bot_id=? AND status IN ('open','new','placing')",
                            (bot_id,)
                        ).fetchone()[0]
                        if open_order_count == 0:
                            results['ghost_virtual'].append(bot_id)
                            self._heal_ghost_virtual(bot_id, name, cycle_id, conn)

            # PAIR_ORPHAN_PHYSICAL: exchange holds, virtual says flat
            elif abs(phys) > tolerance and abs(virt) <= tolerance:
                # Find any bot_id for this pair (just for reporting)
                reporter_bot = pair
                for bot in non_stuck_bots:
                    if bot[2] == pair:
                        reporter_bot = f"{bot[1]} ({bot[0]})"
                        break
                results['orphan_physical'].append(f"{pair}:{phys:.6f}")
                logger.warning(
                    f"[GTR-INV31] PAIR_ORPHAN_PHYSICAL: pair={pair} (ref: {reporter_bot}) "
                    f"virtual is flat but exchange has physical={phys:.6f}. "
                    f"parity_gates will handle on next cycle."
                )

            # PAIR_DRIFT: both nonzero but amounts differ
            elif abs(phys) > tolerance and abs(virt) > tolerance:
                try:
                    # Get pair name (canonical symbol like BTC/USDC:USDC)
                    pair_symbol = pair
                    for bot in non_stuck_bots:
                        if bot[2] == pair:
                            pair_row = conn.execute("SELECT pair FROM bots WHERE id=?", (bot[0],)).fetchone()
                            if pair_row:
                                pair_symbol = pair_row[0]
                            break
                    current_price = float(exchange.get_last_price(pair_symbol) or 0.0)
                except Exception:
                    current_price = 0.0
                usd_val = abs(drift) * current_price
                results['hedge_drift'].append(f"{pair}:drift={drift:.6f}:usd={usd_val:.2f}")
                logger.warning(
                    f"[GTR-INV31] PAIR_DRIFT: pair={pair} drift={drift:.6f} units (USD value ~${usd_val:.2f}). "
                    f"Physical={phys:.6f}, Virtual={virt:.6f}."
                )

        # ── Step 6: Write-through update of active_positions table ──────────
        self._refresh_active_positions(positions, conn)

        return results

    def _get_tolerance_for_pair(self, pair: str, bots: list, conn, exchange) -> float:
        # Find a bot_id to query the exchange precision symbol
        bot_id = None
        for bot in bots:
            if bot[2] == pair:
                bot_id = bot[0]
                break
        if not bot_id:
            row = conn.execute("SELECT id FROM bots WHERE normalized_pair=? LIMIT 1", (pair,)).fetchone()
            if row:
                bot_id = row[0]
        if bot_id:
            try:
                pair_name = conn.execute("SELECT pair FROM bots WHERE id=?", (bot_id,)).fetchone()[0]
                prec = exchange.get_symbol_precision(pair_name)
                return float(prec.get('step_size', 0.001) or 0.001)
            except Exception:
                pass
        return 0.001

    def _build_physical_net(self, positions) -> Dict[str, float]:
        from engine.exchange_interface import normalize_symbol
        result = {}
        for p in (positions or []):
            sym = normalize_symbol(p.get('symbol', ''))
            if not sym:
                continue

            pos_amt = None

            # 1. Try raw Binance positionAmt first (always signed)
            raw_info = p.get('info', {})
            raw_pa = raw_info.get('positionAmt', raw_info.get('positionAmount'))
            if raw_pa is not None:
                pos_amt = float(raw_pa)

            # 2. Try top-level positionAmt (always signed)
            if pos_amt is None or pos_amt == 0:
                raw_pa_top = p.get('positionAmt', p.get('positionAmount'))
                if raw_pa_top is not None:
                    pos_amt = float(raw_pa_top)

            # 3. Try CCXT contracts (signed in some versions)
            if pos_amt is None or pos_amt == 0:
                raw_contracts = p.get('contracts')
                if raw_contracts is not None:
                    pos_amt = float(raw_contracts)

            # 3b. Try CCXT qty/size (for compatibility/mocks)
            if pos_amt is None or pos_amt == 0:
                raw_qty = p.get('qty', p.get('size'))
                if raw_qty is not None:
                    pos_amt = float(raw_qty)

            # If the detected pos_amt is positive but side is explicitly SHORT, correct the sign
            if pos_amt is not None and pos_amt > 0:
                side = str(p.get('side', '')).upper()
                if side == 'SHORT':
                    pos_amt = -pos_amt

            # 4. Fall back to side field only if positionAmt/contracts/qty unavailable
            if pos_amt is None or pos_amt == 0:
                side = str(p.get('side', '')).upper()
                size = float(p.get('contracts', 0) or 
                             p.get('qty', 0) or
                             p.get('size', 0) or
                             abs(float(p.get('positionAmt', 0) or p.get('info', {}).get('positionAmt', 0))))
                pos_amt = size if side == 'LONG' else -size

            # pos_amt is now signed: positive=LONG, negative=SHORT
            if abs(pos_amt) > 1e-9:
                result[sym] = result.get(sym, 0.0) + pos_amt
        return result

    def _refresh_active_positions(self, positions, conn):
        """
        Refresh active_positions table to mirror the exchange reality.
        """
        from engine.exchange_interface import normalize_symbol
        from engine.database import (
            get_pair_virtual_net,
            recompute_invested_from_orders,
            get_active_bot_id_by_symbol_direction
        )
        try:
            conn.execute("DELETE FROM active_positions")
            agg_positions = {}
            for p in (positions or []):
                raw_symbol = p.get('symbol', 'UNKNOWN')
                symbol = normalize_symbol(raw_symbol)
                amount = float(p.get('contracts', 0) or p.get('size', 0) or p.get('positionAmt', 0) or 0)
                entry_price = float(p.get('entryPrice', 0) or 0)
                if abs(amount) == 0:
                    continue

                side = 'LONG' if amount > 0 else 'SHORT'
                key = (symbol, side)
                if key not in agg_positions:
                    agg_positions[key] = {'size': 0.0, 'value': 0.0}
                agg_positions[key]['size'] += abs(amount)
                agg_positions[key]['value'] += abs(amount) * abs(entry_price)

            ts = int(time.time())
            for (symbol, side), data in agg_positions.items():
                v_net = get_pair_virtual_net(symbol)

                # Fetch active bots
                cursor = conn.execute("""
                    SELECT b.id, b.direction, t.avg_entry_price
                    FROM bots b JOIN trades t ON b.id = t.bot_id
                    WHERE b.is_active = 1 AND (b.pair = ? OR b.normalized_pair = ?)
                """, (symbol, symbol))
                bots = cursor.fetchall()

                bot_shares = []
                for b_id, b_dir, b_avg in bots:
                    _, _, net_qty, _ = recompute_invested_from_orders(b_id)
                    share_qty = max(0.0, net_qty)
                    bot_shares.append({'id': b_id, 'dir': b_dir.upper(), 'qty': share_qty, 'avg': float(b_avg or 0)})

                ph_net = data['size'] if side == 'LONG' else -data['size']
                if abs(v_net - ph_net) < 0.001:
                    for share in bot_shares:
                        if share['qty'] > 0:
                            conn.execute("""
                                INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (share['id'], symbol, share['dir'], share['qty'], share['avg'], ts))
                else:
                    avg_price = data['value'] / data['size'] if data['size'] > 0 else 0
                    owner_id = get_active_bot_id_by_symbol_direction(symbol, side) or 0
                    conn.execute("""
                        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (owner_id, symbol, side, data['size'], avg_price, ts))

            if not agg_positions:
                conn.execute("""
                    INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (0, 'GLOBAL', 'FLAT', 0.0, 0.0, ts))

            conn.commit()
        except Exception as e:
            logger.error(f"[GTR] Failed to refresh active_positions: {e}")

    def _heal_ghost_virtual(self, bot_id, name, cycle_id, conn):
        """
        Physical position is gone but virtual ledger still shows it.
        Force-zero the ledger and reset to Scanning.
        """
        logger.critical(
            f"[GTR-INV31] GHOST_VIRTUAL: Bot {name} ({bot_id}) "
            f"has virtual position but physical=0 and no open orders. "
            f"Force-resetting ledger to Scanning."
        )
        conn.execute("""
            UPDATE trades SET 
                open_qty=0, total_invested=0, avg_entry_price=0,
                current_step=0, entry_confirmed=0,
                cycle_id = cycle_id + 1
            WHERE bot_id=?
        """, (bot_id,))
        conn.execute(
            "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
        )
        conn.execute("""
            UPDATE bot_orders SET status='reset_cleared', updated_at=?
            WHERE bot_id=? AND cycle_id=?
            AND status NOT IN ('reset_cleared','auto_closed','filled','cancelled')
        """, (int(time.time()), bot_id, cycle_id))
        conn.commit()
        logger.warning(
            f"[GTR-INV31] Bot {name} ({bot_id}): Ghost virtual cleared. "
            f"cycle_id incremented to {cycle_id+1}. Status → Scanning."
        )

    def _heal_stuck_cascade(self, bot_id, name, status, open_qty,
                             norm_pair, exchange, conn):
        """
        Bot has been in a transitional cascade status for > CASCADE_TIMEOUT.
        Re-trigger the appropriate completion.
        """
        logger.critical(
            f"[GTR-INV31] STUCK_CASCADE: Bot {name} ({bot_id}) "
            f"has been in '{status}' for >{self.CASCADE_TIMEOUT}s. "
            f"Re-triggering cascade completion."
        )
        if status in ('pending_close', 'FLATTENING') and open_qty <= 0.001:
            # Position already closed, just needs DB reset
            from engine.database import reset_bot_after_tp
            reset_bot_after_tp(bot_id, exit_price=0.0, 
                               action_label='GTR_STUCK_CASCADE_RECOVERY')
            logger.warning(
                f"[GTR-INV31] Bot {name}: pending_close with open_qty=0. "
                f"Forced reset_bot_after_tp. Status → Scanning."
            )
        elif status == 'pending_hedge_close' and open_qty <= 0.001:
            # Parent waiting for child that already closed
            conn.execute(
                "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (bot_id,)
            )
            conn.execute("""
                UPDATE trades SET cycle_id=cycle_id+1, current_step=0,
                open_qty=0, total_invested=0, avg_entry_price=0,
                entry_confirmed=0 WHERE bot_id=?
            """, (bot_id,))
            conn.commit()
            logger.warning(
                f"[GTR-INV31] Bot {name}: pending_hedge_close with open_qty=0. "
                f"Forced Scanning reset. cycle_id incremented."
            )
        elif status in ('pending_close', 'FLATTENING') and open_qty > 0.001:
            # Position still exists — re-trigger flatten
            logger.warning(
                f"[GTR-INV31] Bot {name}: stuck {status} with open_qty={open_qty}. "
                f"Setting pending_flatten for runner to re-execute close."
            )
            conn.execute(
                "UPDATE bots SET status='pending_flatten', cascade_started_at=? WHERE id=?", (int(time.time()), bot_id)
            )
            conn.commit()
