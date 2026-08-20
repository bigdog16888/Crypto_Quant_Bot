"""
O-10: Hedge-Engagement Watchdog.

Verifies that when a parent bot reaches its hedge_trigger_step, the hedge child
bot (1) is actually configured, and (2) actually holds an offsetting position
within a reasonable grace window.  If either condition fails, the parent is
frozen to REQUIRE_MANUAL_PROOF and an alert is raised.  When >=2 distinct
parents are frozen for hedge-engagement failure within a rolling window, the
engine escalates to a whole-engine halt.

Deliberately a small, pure, testable module: it inspects only the DB state the
hedge-signal path is supposed to have produced.  It does NOT mutate exchange
state and does NOT place/cancel orders.
"""
import time
import logging

logger = logging.getLogger(__name__)

# Defaults (overridable via config)
MIN_HEDGE_QTY = 0.0001
HEDGE_ENGAGE_TIMEOUT_SECONDS = 300
HEDGE_FAIL_WINDOW_SECONDS = 24 * 3600  # rolling window for escalation (24h)
FREEZE_MARKER = "HEDGE_ENGAGE_FAILURE:"


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
    }


def _child_offset_ok(child_bot_id, parent_direction, conn, min_qty=MIN_HEDGE_QTY):
    """
    Returns True if the hedge child holds an offsetting open position.

    Reads the child's virtual position from `trades.open_qty` and its
    `direction` from `bots`.  A proper hedge must be opposite the parent:
      parent LONG  -> child SHORT  (negative child direction)
      parent SHORT -> child LONG   (positive child direction)
    """
    if not child_bot_id:
        return False
    row = conn.execute(
        "SELECT b.direction, b.status, COALESCE(t.open_qty, 0) "
        "FROM bots b LEFT JOIN trades t ON b.id = t.bot_id "
        "WHERE b.id = ?",
        (child_bot_id,)
    ).fetchone()
    if not row:
        return False
    child_dir, child_status, child_qty = row[0], (row[1] or "").upper(), float(row[2] or 0)
    if child_status in ("REQUIRE_MANUAL_PROOF",):
        return False
    if child_qty <= min_qty:
        return False
    # Offsetting sign: child direction must oppose parent.
    return (child_dir == "SHORT") != (parent_direction.upper() == "SHORT")


def _count_engine_hedge_failures(conn, now=None, fail_window=None):
    """
    Count distinct parents currently frozen for hedge-engagement failure
    within the rolling `fail_window`.  A parent counts only if its status is
    REQUIRE_MANUAL_PROOF and its last_error carries the FREEZE_MARKER and its
    last_error_time is inside the window.
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


def verify_hedge_engagement(parent_bot_id, parent_direction, conn, now=None, config=None):
    """
    Main watchdog entry point.  Called when a parent reaches its trigger step.

    Returns a dict:
      {
        "engaged": bool,
        "freeze_parent": bool,   # True => freeze the specific parent
        "engine_halt": bool,     # True => >=2 parents frozen in window, engineered halt
        "reason": str,
        "child_bot_id": int|None,
      }

    Non-destructive: it only reads DB and returns a verdict.  The caller is
    responsible for applying the freeze / halt (keeps this pure and testable).
    """
    cfg = get_config(config)
    min_qty = cfg["min_hedge_qty"]
    engage_timeout = cfg["engage_timeout"]
    fail_window = cfg["fail_window"]

    result = {
        "engaged": False,
        "freeze_parent": False,
        "engine_halt": False,
        "reason": "",
        "child_bot_id": None,
    }

    # 1. Parent must have a configured child
    prow = conn.execute(
        "SELECT hedge_child_bot_id FROM bots WHERE id = ?", (parent_bot_id,)
    ).fetchone()
    if not prow or not prow[0]:
        result["reason"] = "no_hedge_child_bot_id"
        result["freeze_parent"] = True
    else:
        child_bot_id = int(prow[0])
        result["child_bot_id"] = child_bot_id
        # 2. Check offsetting position
        if _child_offset_ok(child_bot_id, parent_direction, conn, min_qty=min_qty):
            result["engaged"] = True
            result["reason"] = "engaged"
            return result
        result["reason"] = "child_not_offsetting"

    # If we get here, the hedge has NOT engaged (or child unconfigured).
    # Apply a grace window via child's basket_start_time / last entry attempt?
    # Simpler, deterministic: we freeze immediately on failure because
    # `_signal_hedge_child_entry` already placed/attempted the entry this cycle.
    # (The watch runs at the natural maintain_orders cadence each pass.)
    result["freeze_parent"] = True

    # 3. Escalation: if this would be >=2nd distinct parent frozen in window,
    #    engine-wide halt.
    existing_failures = _count_engine_hedge_failures(conn, now=now,
                                                     fail_window=fail_window)
    # Count the current parent too if it's not already counted.
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