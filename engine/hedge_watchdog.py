"""
O-10: Hedge-Engagement Watchdog (One-Way Netting Aware).

Verifies that for each pair with hedge children, the aggregate virtual position
across ALL active bots matches the exchange's real net position within tolerance.

This is the one-way netting invariant: sum of signed virtual open_qty == exchange net.
If they diverge, netting has broken down and the parent(s) must be frozen.

Deliberately small, pure, testable: it reads DB state and exchange state,
returns a verdict. Caller applies freeze/halt.
"""
import time
import logging

logger = logging.getLogger(__name__)

# Defaults (overridable via config)
MIN_HEDGE_QTY = 0.0001
HEDGE_ENGAGE_TIMEOUT_SECONDS = 300
HEDGE_FAIL_WINDOW_SECONDS = 24 * 3600  # rolling window for escalation (24h)
FREEZE_MARKER = "HEDGE_ENGAGE_FAILURE:"
# Pair-level netting tolerance — separate from child fill check
PAIR_NETTING_TOLERANCE = 0.002


def get_config(config=None):
    """Resolve watchdog parameters from config, with safe defaults."""
    cfg = config or {}
    def _g(name, default):
        v = getattr(cfg, name, None) if not isinstance(cfg, dict) else cfg.get(name)
        return v if v is not None else default
    return {
        "min_hedge_qty": _g("MIN_HEDGE_QTY", MIN_HEDGE_QTY),
        "engage_timeout": _g("HEDGE_ENGAGE_TIMEOUT_SECONDS", HEDGE_ENGAGE_TIMEOUT_SECONDS),
        "fail_window": _g("HEDGE_FAIL_WINDOW_SECONDS", HEDGE_FAIL_WINDOW_SECONDS),
        "pair_netting_tolerance": _g("PAIR_NETTING_TOLERANCE", PAIR_NETTING_TOLERANCE),
    }


def _count_engine_hedge_failures(conn, now=None, fail_window=None):
    """
    Count distinct parents currently frozen for hedge-engagement failure
    within the rolling `fail_window`.
    """
    now = now if now is not None else time.time()
    fail_window = fail_window or HEDGE_FAIL_WINDOW_SECONDS
    cutoff = now - fail_window
    rows = conn.execute(
        "SELECT id, last_error_time, last_error FROM bots "
        "WHERE status = 'REQUIRE_MANUAL_PROOF' "
        "AND last_error IS NOT NULL "
        "AND last_error LIKE ?",
        (FREEZE_MARKER + "%",)
    ).fetchall()
    count = 0
    for bid, ts, err in rows:
        if ts is None:
            continue
        if float(ts) >= cutoff:
            count += 1
    return count


def verify_netting_engagement(parent_bot_id, parent_direction, conn, exchange, config=None):
    """
    Main watchdog entry point — pair-level netting verification.

    Called when a parent reaches its trigger step (opportunistic check).
    Also called periodically by reconciler for all active pairs.

    Returns a dict:
      {
        "engaged": bool,           # True if pair netting is within tolerance
        "freeze_parent": bool,     # True => freeze the specific parent
        "engine_halt": bool,       # True => >=2 parents frozen in window
        "reason": str,             # "netting_ok" | "netting_diverged" | "no_hedge_child" | "exchange_unavailable"
        "child_bot_id": int|None,
        "virtual_net": float,      # Sum of signed open_qty across all active bots on pair
        "physical_net": float,     # Exchange's actual net position
        "delta": float,            # physical - virtual
        "pair": str,               # The pair symbol
      }
    """
    cfg = get_config(config)
    tol = cfg["pair_netting_tolerance"]
    fail_window = cfg["fail_window"]

    result = {
        "engaged": False,
        "freeze_parent": False,
        "engine_halt": False,
        "reason": "",
        "child_bot_id": None,
        "virtual_net": 0.0,
        "physical_net": 0.0,
        "delta": 0.0,
        "pair": "",
    }

    # 1. Parent must have a configured child (still required for signal path)
    prow = conn.execute(
        "SELECT hedge_child_bot_id, pair FROM bots WHERE id = ?", (parent_bot_id,)
    ).fetchone()
    if not prow or not prow[0]:
        result["reason"] = "no_hedge_child_bot_id"
        result["freeze_parent"] = True
        return result

    child_bot_id = int(prow[0])
    pair = prow[1]
    result["child_bot_id"] = child_bot_id
    result["pair"] = pair

    # 2. NEW: Verify PAIR-LEVEL NETTING (the real hedge signal for one-way mode)
    try:
        from engine.parity_gates import pair_parity_ok
        ok, virtual, physical, delta = pair_parity_ok(pair, exchange=exchange, tol=tol)
        result["virtual_net"] = virtual
        result["physical_net"] = physical
        result["delta"] = delta

        if ok:
            # Netting is working: virtual sum of all bots = exchange net
            result["engaged"] = True
            result["reason"] = "netting_ok"
            return result
        else:
            # Netting broken: virtual ≠ physical beyond tolerance
            result["reason"] = (
                f"netting_diverged delta={delta:.6f} "
                f"(virtual={virtual:.6f} vs physical={physical:.6f})"
            )
            result["freeze_parent"] = True

    except Exception as e:
        logger.warning(f"[O-10] Pair netting check failed for {pair}: {e}")
        result["reason"] = f"exchange_unavailable: {e}"
        result["freeze_parent"] = True  # fail-closed

    # 3. Escalation: if this would be >=2nd distinct parent frozen in window
    existing_failures = _count_engine_hedge_failures(conn, now=time.time(), fail_window=fail_window)
    already_frozen = False
    prow2 = conn.execute(
        "SELECT last_error, last_error_time FROM bots WHERE id = ?",
        (parent_bot_id,)
    ).fetchone()
    if prow2 and prow2[0] and prow2[0].startswith(FREEZE_MARKER):
        already_frozen = True
    if existing_failures + (0 if already_frozen else 1) >= 2:
        result["engine_halt"] = True

    return result


def verify_all_pairs_netting(conn, exchange, config=None):
    """
    Periodic check: verify netting for ALL active pairs with hedge children.
    
    Called by reconciler/cycle loop, not tied to any specific bot's cycle.
    Returns list of results (one per pair with hedge children).
    """
    cfg = get_config(config)
    tol = cfg["pair_netting_tolerance"]
    
    # Find all active pairs that have at least one parent with a hedge child
    conn_local = conn
    rows = conn_local.execute("""
        SELECT DISTINCT b.pair, b.id as parent_id, b.hedge_child_bot_id, b.direction
        FROM bots b
        WHERE b.is_active = 1 
          AND b.hedge_child_bot_id IS NOT NULL
          AND b.bot_type != 'hedge_child'
    """).fetchall()
    
    results = []
    for pair, parent_id, child_id, direction in rows:
        result = verify_netting_engagement(parent_id, direction, conn_local, exchange, config)
        results.append(result)
    
    return results


# Backward compatibility — OLD FUNCTION REMOVED
# verify_hedge_engagement no longer exists — use verify_netting_engagement instead
# The old _child_offset_ok (child fill check) is also removed.
# The new logic checks PAIR-LEVEL NETTING, not individual child fills.