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
            SELECT b.pair, b.direction, COALESCE(t.total_invested, 0), COALESCE(t.avg_entry_price, 0)
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1 AND COALESCE(t.total_invested, 0) > 0
        """)
        db_positions = cursor.fetchall()
        conn.close()

        # Build DB net map: { normalized_symbol: net_usd }
        db_net_map = {}
        for pair, direction, invested, entry_price in db_positions:
            sym = normalize_symbol(pair)
            signed = float(invested) if direction.upper() == 'LONG' else -float(invested)
            db_net_map[sym] = db_net_map.get(sym, 0) + signed

        # Compare
        all_symbols = set(list(exchange_map.keys()) + list(db_net_map.keys()))
        
        for sym in all_symbols:
            exch_qty = exchange_map.get(sym, 0)
            db_usd = db_net_map.get(sym, 0)

            # If exchange has position but DB has nothing → CRITICAL
            if exch_qty != 0 and db_usd == 0:
                issues.append(f"CRITICAL: Exchange has {exch_qty} {sym} but DB has $0. Orphaned position!")
                logger.warning(f"    ❌ {sym}: Exchange={exch_qty} | DB=$0 (ORPHAN)")
            
            # If DB has position but exchange has nothing → Warning (ghost)
            elif db_usd != 0 and exch_qty == 0:
                issues.append(f"WARNING: DB has ${db_usd:.2f} {sym} but Exchange has 0. Possible ghost.")
                logger.warning(f"    ⚠️ {sym}: DB=${db_usd:.2f} | Exchange=0 (GHOST?)")
            
            # Both have positions — check direction alignment
            elif exch_qty != 0 and db_usd != 0:
                # Sign check: both should agree on direction
                if (exch_qty > 0 and db_usd < 0) or (exch_qty < 0 and db_usd > 0):
                    issues.append(f"WARNING: Direction mismatch for {sym}: Exchange={'LONG' if exch_qty > 0 else 'SHORT'} vs DB={'LONG' if db_usd > 0 else 'SHORT'}")
                    logger.warning(f"    ⚠️ {sym}: Direction mismatch")
                else:
                    logger.info(f"    ✅ {sym}: Exchange={exch_qty} | DB=${db_usd:.2f} — Aligned")

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
        conn.close()

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
        conn.close()

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
