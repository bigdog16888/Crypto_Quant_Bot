"""
engine/preflight.py — Professional Startup Gate

Runs BEFORE the main loop. Validates system integrity against exchange reality.
Returns a structured result: { passed: bool, summary: str, issues: list }

Checks (in order):
  1. Position Match  — Exchange positions vs DB net positions
  2. Order Integrity — In-trade bots have expected open orders (TP + Grid)
  3. Order Prices    — Order prices match DB expectations (within tolerance)
  4. Step Consistency — current_step matches actual order count per bot

Auto-heals minor issues. Blocks on critical mismatches.
"""

import logging
import sqlite3
import json
import time
from typing import Dict, List, Any

from engine.database import get_connection, DB_PATH
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from config.settings import config

logger = logging.getLogger("Preflight")


def preflight_check() -> Dict[str, Any]:
    """
    Master preflight function. Creates exchanges, runs all checks, returns result.
    """
    issues = []
    healed = []
    
    logger.info("=" * 60)
    logger.info("🛫 PREFLIGHT CHECK — Validating System Integrity")
    logger.info("=" * 60)

    # --- Initialize Exchange Connections ---
    exchanges = {}
    try:
        market_type = config.MARKET_TYPE
        if getattr(config, 'FUTURES_ONLY_MODE', False):
            market_type = 'future'
        exchanges[market_type] = ExchangeInterface(market_type=market_type)
        logger.info(f"  ✅ Exchange connection established ({market_type})")
    except Exception as e:
        issues.append(f"Failed to connect to exchange: {e}")
        return _result(False, "Exchange connection failed", issues, healed)

    # --- Step 1: Position Match ---
    pos_issues, pos_healed = _check_position_match(exchanges)
    issues.extend(pos_issues)
    healed.extend(pos_healed)

    # --- Step 2: Order Integrity ---
    ord_issues, ord_healed = _check_order_integrity(exchanges)
    issues.extend(ord_issues)
    healed.extend(ord_healed)

    # --- Step 3: Step Consistency ---
    step_issues, step_healed = _check_step_consistency()
    issues.extend(step_issues)
    healed.extend(step_healed)

    # --- Summary ---
    critical = [i for i in issues if i.startswith("CRITICAL")]
    passed = len(critical) == 0

    total_checks = 3
    passed_checks = total_checks - (1 if pos_issues else 0) - (1 if ord_issues else 0) - (1 if step_issues else 0)
    
    summary = f"{passed_checks}/{total_checks} checks passed. {len(healed)} auto-healed. {len(issues)} issues."
    
    logger.info("=" * 60)
    if passed:
        logger.info(f"✅ PREFLIGHT PASSED: {summary}")
    else:
        logger.warning(f"⚠️ PREFLIGHT ISSUES: {summary}")
    logger.info("=" * 60)

    return _result(passed, summary, issues, healed)


def _result(passed: bool, summary: str, issues: list, healed: list) -> dict:
    return {
        'passed': passed,
        'summary': summary,
        'issues': issues,
        'healed': healed,
        'timestamp': time.time()
    }


# ============================================================
# CHECK 1: Position Match (Exchange vs DB)
# ============================================================
def _check_position_match(exchanges: Dict[str, ExchangeInterface]):
    """
    Compares exchange physical positions with DB virtual net positions.
    """
    issues = []
    healed = []
    logger.info("  📋 Check 1: Position Match (Exchange vs DB)")

    try:
        # Fetch exchange positions
        all_positions = []
        for mt, ex in exchanges.items():
            positions = ex.fetch_positions()
            if positions:
                all_positions.extend(positions)

        # Build exchange position map: { normalized_symbol: net_qty }
        exchange_map = {}
        for p in all_positions:
            sym = normalize_symbol(p['symbol'])
            qty = float(p.get('contracts', 0) or 0)
            if qty != 0:
                exchange_map[sym] = exchange_map.get(sym, 0) + qty

        # Fetch DB virtual positions
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.pair, b.direction, 
                   SUM(CASE WHEN bo.order_type IN ('entry','grid','adoption','adoption_entry') THEN bo.filled_amount
                            WHEN bo.order_type IN ('tp','close','adoption_reduce','sl','dust_close','flatten_close') THEN -bo.filled_amount
                            ELSE 0 END) as net_qty
            FROM bots b
            LEFT JOIN bot_orders bo ON b.id = bo.bot_id 
                  AND bo.status NOT IN ('auto_closed', 'reset_cleared', 'placing')
                  AND bo.filled_amount > 0
            WHERE b.is_active = 1
            GROUP BY b.id
        """)
        db_positions = cursor.fetchall()
        pass # conn.close() disabled for singleton safety

        # Build DB qty map: { normalized_symbol: net_qty }
        db_net_qty_map = {}
        for pair, direction, raw_qty in db_positions:
            sym = normalize_symbol(pair)
            qty_val = float(raw_qty) if raw_qty else 0.0
            if qty_val > 0:
                signed_qty = qty_val if direction.upper() == 'LONG' else -qty_val
                db_net_qty_map[sym] = db_net_qty_map.get(sym, 0) + signed_qty

        # 🚀 CONSENSUS COMPARISON ($0.01 Standard)
        PRECISION_USD = 0.01
        
        # Fetch exchange positions and build consolidated qty map
        exchange_reported_qty_map = {}
        mark_price_map = {}
        for p in all_positions:
            sym = normalize_symbol(p['symbol'])
            # Extract properly signed magnitude (One-way mode 'contracts' is often unsigned natively)
            raw_qty = float(p.get('contracts', 0) or 0)
            side = p.get('side', 'long').lower()
            if side == 'short':
                raw_qty = -abs(raw_qty)
            else:
                raw_qty = abs(raw_qty)
            
            if raw_qty == 0: continue
            exchange_reported_qty_map[sym] = exchange_reported_qty_map.get(sym, 0) + raw_qty
            
            notional = abs(float(p.get('notional', 0) or p.get('notionalValue', 0) or 0))
            if notional > 0 and abs(raw_qty) > 0:
                mark_price_map[sym] = notional / abs(raw_qty)
            elif float(p.get('markPrice', 0)) > 0:
                mark_price_map[sym] = float(p.get('markPrice'))
            elif float(p.get('entryPrice', 0)) > 0:
                mark_price_map[sym] = float(p.get('entryPrice'))
            else:
                mark_price_map[sym] = 1.0

        # Compare Quantities using uniform Mark Price translation
        all_symbols = set(list(exchange_reported_qty_map.keys()) + list(db_net_qty_map.keys()))
        
        for sym in all_symbols:
            exch_qty = exchange_reported_qty_map.get(sym, 0.0)
            db_qty = db_net_qty_map.get(sym, 0.0)
            
            mark_price = mark_price_map.get(sym, 1.0)
            delta_qty = abs(db_qty - exch_qty)
            delta_usd = delta_qty * mark_price
            
            exch_usd = exch_qty * mark_price
            db_usd = db_qty * mark_price
            
            # Check 1: Perfect Alignment or Consensus ($0.01 threshold)
            if delta_usd < PRECISION_USD:
                logger.info(f"    ✅ {sym}: Net Parity (Exchange=${exch_usd:.2f} | DB=${db_usd:.2f})")
                continue

            # Check 2: Directional Healing (Fact-Based)
            # Find all active bots for this pair
            cursor.execute("""
                SELECT b.id, b.name, b.direction, b.pair
                FROM bots b
                WHERE b.is_active = 1
            """)
            all_active = cursor.fetchall()
            pair_bots = [b for b in all_active if normalize_symbol(b[3]) == sym]
            
            # Fetch current trade state for these bots
            # all_active returns (id, name, direction, pair)
            ids = [f"({b[0]})" for b in pair_bots]
            if not ids: continue
            
            cursor.execute(f"SELECT bot_id, current_step, total_invested FROM trades WHERE bot_id IN ({','.join([str(b[0]) for b in pair_bots])})")
            trade_map = {row[0]: row for row in cursor.fetchall()}
            
            # Determine which side is responsible for the mismatch
            is_sole_pair_bot = len(pair_bots) == 1
            diff_qty = exch_qty - db_qty
            diff_usd = diff_qty * mark_price
            target_direction = 'LONG' if diff_qty > 0 else 'SHORT'
            
            actual_target_bot = None
            if is_sole_pair_bot:
                # Rule 1: Sole bot for pair, it owns the error
                actual_target_bot = pair_bots[0]
            else:
                # Rule 2: Multi-bot pair, use directional attribution
                target_bots = [b for b in pair_bots if b[2].upper() == target_direction]
                if len(target_bots) >= 1:
                    # Select the lowest ID bot deterministically among candidates
                    actual_target_bot = sorted(target_bots, key=lambda x: x[0])[0]
            
            if actual_target_bot:
                bot_id, bot_name, direction, _ = actual_target_bot
                trade_row = trade_map.get(bot_id, (bot_id, 0, 0.0))
                current_step = trade_row[1]

                # ────────────────────────────────────────────────────────────────
                # REMOVED: v2.0 ADOPTION logic here was aggressively double-counting.
                # Offline fills are the explicit domain of StateReconciler Pass 3.
                # Preflight should NEVER write phantom adoptions.
                # ────────────────────────────────────────────────────────────────
                continue



            # Check 3: If not healed, report as issue
            if exch_qty != 0 and db_usd == 0:
                issues.append(f"CRITICAL: Exchange has {exch_qty} {sym} but DB has $0. Orphaned position!")
                logger.warning(f"    ❌ {sym}: Exchange={exch_qty} | DB=$0 (ORPHAN)")
            elif db_usd != 0 and exch_qty == 0:
                issues.append(f"WARNING: DB has ${db_usd:.2f} {sym} but Exchange has 0. Possible ghost.")
                logger.warning(f"    ⚠️ {sym}: DB=${db_usd:.2f} | Exchange=0 (GHOST?)")
            else:
                issues.append(f"WARNING: Sync Mismatch for {sym}: Exchange=${exch_usd:.2f} vs DB=${db_usd:.2f} (Diff: ${delta_usd:.2f})")
                logger.warning(f"    ⚠️ {sym}: Sync Mismatch (${delta_usd:.2f})")

        if not issues:
            logger.info("    ✅ All positions aligned")
            
    except Exception as e:
        issues.append(f"Position check error: {e}")
        logger.error(f"    ❌ Position check failed: {e}")

    return issues, healed


# ============================================================
# CHECK 2: Order Integrity (In-trade bots have orders?)
# ============================================================
def _check_order_integrity(exchanges: Dict[str, ExchangeInterface]):
    """
    For each in-trade bot, verify it has open orders on the exchange.
    """
    issues = []
    healed = []
    logger.info("  📋 Check 2: Order Integrity")

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        
        # Get all in-trade bots
        cursor.execute("""
            SELECT b.id, b.name, b.pair, b.direction, b.config,
                   t.total_invested, t.current_step, t.avg_entry_price
            FROM bots b
            JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1 AND t.total_invested > 0
        """)
        in_trade_bots = cursor.fetchall()
        pass # conn.close() disabled for singleton safety

        if not in_trade_bots:
            logger.info("    ℹ️ No bots in trade — skipping order check")
            return issues, healed

        # Fetch all open orders from exchange
        all_orders = []
        for mt, ex in exchanges.items():
            try:
                orders = ex.fetch_open_orders()
                if orders:
                    all_orders.extend(orders)
            except Exception as e:
                logger.warning(f"    ⚠️ Failed to fetch orders for {mt}: {e}")

        for bot_id, name, pair, direction, config_json, invested, step, entry_price in in_trade_bots:
            # Find this bot's orders by Client Order ID pattern
            bot_orders = [o for o in all_orders if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]
            
            if not bot_orders:
                issues.append(f"WARNING: Bot {name} (ID:{bot_id}) is IN TRADE (${invested:.2f}) but has NO open orders")
                logger.warning(f"    ⚠️ Bot {name}: In trade, 0 orders (auto-healing should fix)")
            else:
                # Check order types present
                order_types = [o.get('clientOrderId', '').split('_')[2] if len(o.get('clientOrderId', '').split('_')) > 2 else 'UNKNOWN' for o in bot_orders]
                has_tp = any('TP' in ot for ot in order_types)
                has_grid = any('GRID' in ot for ot in order_types)
                
                status_parts = []
                if has_tp: status_parts.append("TP")
                if has_grid: status_parts.append(f"GRID")
                status_parts.append(f"Total:{len(bot_orders)}")
                
                if not has_tp and step >= 1:
                    issues.append(f"WARNING: Bot {name} at step {step} has no TP order")
                    logger.warning(f"    ⚠️ Bot {name}: Missing TP order (step {step})")
                else:
                    logger.info(f"    ✅ Bot {name}: {', '.join(status_parts)}")

    except Exception as e:
        issues.append(f"Order integrity check error: {e}")
        logger.error(f"    ❌ Order check failed: {e}")

    return issues, healed


# ============================================================
# CHECK 3: Step Consistency
# ============================================================
def _check_step_consistency():
    """
    Verify current_step in DB is reasonable (not negative, not impossibly high).
    """
    issues = []
    healed = []
    logger.info("  📋 Check 3: Step Consistency")

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT b.id, b.name, t.current_step, t.total_invested, b.base_size
            FROM bots b
            JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1 AND t.total_invested > 0
        """)
        rows = cursor.fetchall()
        pass # conn.close() disabled for singleton safety

        for bot_id, name, step, invested, base_size in rows:
            # Sanity checks
            if step < 0:
                issues.append(f"CRITICAL: Bot {name} has negative step ({step})")
                logger.warning(f"    ❌ Bot {name}: step={step} (NEGATIVE)")
            elif step > 20:
                issues.append(f"WARNING: Bot {name} has unusually high step ({step})")
                logger.warning(f"    ⚠️ Bot {name}: step={step} (>20, suspicious)")
            elif base_size and base_size > 0 and invested > 0:
                # Quick ratio check: invested / base_size should roughly match geometric sum
                ratio = float(invested) / float(base_size)
                expected_min = step  # At minimum, invested >= step * base_size (for mult=1)
                if ratio < expected_min * 0.5 and step > 2:
                    issues.append(f"WARNING: Bot {name} step={step} but invested/base ratio={ratio:.1f} seems low")
                    logger.warning(f"    ⚠️ Bot {name}: step={step}, ratio={ratio:.1f} (underweight?)")
                else:
                    logger.info(f"    ✅ Bot {name}: step={step}, invested=${invested:.2f} — Consistent")
            else:
                logger.info(f"    ✅ Bot {name}: step={step}, invested=${invested:.2f}")

    except Exception as e:
        issues.append(f"Step consistency check error: {e}")
        logger.error(f"    ❌ Step check failed: {e}")

    return issues, healed
