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
        "child_step": _g("child_step", None),           # optional: child's hedge step number
        "parent_cycle_id": _g("parent_cycle_id", None), # optional: parent's cycle_id
    }


def _child_hedge_qty_from_orders(child_bot_id, child_step, parent_cycle_id, conn):
    """
    Compute the child's hedge position data from bot_orders.

    For the engagement check (filled_qty): sum of filled_amount for filled/closed orders.
    For the grace window (has_entry_order): uses the SAME aggregation as the signal path
    at bot_executor.py:5030-5038 to detect if there's any pending/active order:
      - Single query: CASE WHEN status IN ('open','new','placing','cancelling') THEN amount ELSE filled_amount END
      - has_entry_order = True if this sum > 0 (i.e., any entry/grid order exists)
    - order_type IN ('entry', 'grid')
    - Filter by step == child_step and cycle_id == parent_cycle_id when provided

    Returns (filled_qty, has_entry_order, oldest_entry_created_at, latest_filled_at)
    - filled_qty: actual credited position (what counts as "engaged")
    - has_entry_order: True if signal aggregation > 0 (pending or filled)
    - oldest_entry_created_at: MIN(created_at) across all entry/grid orders
    - latest_filled_at: MAX(filled_at) for filled/closed orders with filled_at > 0
    """
    if not child_bot_id:
        return 0.0, False, None, None

    # Build query dynamically based on available filters
    where_clauses = ["bot_id = ?", "order_type IN ('entry', 'grid')"]
    params = [child_bot_id]
    if child_step is not None:
        where_clauses.append("step = ?")
        params.append(child_step)
    if parent_cycle_id is not None:
        where_clauses.append("cycle_id = ?")
        params.append(parent_cycle_id)

    where_sql = " AND ".join(where_clauses)

    # Filled qty: sum of filled_amount for filled/closed/partially_filled/reset_cleared orders
    filled_qty_row = conn.execute(
        f"SELECT COALESCE(SUM(filled_amount), 0) FROM bot_orders WHERE {where_sql} "
        f"AND status IN ('filled','closed','partially_filled','reset_cleared')",
        params
    ).fetchone()
    filled_qty = float(filled_qty_row[0] or 0.0)

    # has_entry_order: uses signal's aggregation logic
    # If the CASE WHEN sum > 0, there's an active order (pending or filled)
    has_entry_row = conn.execute(
        f"SELECT COALESCE(SUM("
        f"  CASE WHEN status IN ('open','new','placing','cancelling') THEN amount ELSE filled_amount END"
        f"), 0) FROM bot_orders WHERE {where_sql}",
        params
    ).fetchone()
    has_entry_order = float(has_entry_row[0] or 0.0) > 0.0

    # Oldest entry order created_at (for grace window) - considers ALL entry/grid orders
    oldest_created_row = conn.execute(
        f"SELECT MIN(created_at) FROM bot_orders WHERE {where_sql}",
        params
    ).fetchone()
    oldest_created = oldest_created_row[0] if oldest_created_row and oldest_created_row[0] else None

    # Latest filled_at for filled/closed orders (for grace window when filled)
    latest_filled_row = conn.execute(
        f"SELECT MAX(filled_at) FROM bot_orders WHERE {where_sql} "
        f"AND status IN ('filled','closed','partially_filled','reset_cleared') "
        f"AND filled_at > 0",
        params
    ).fetchone()
    latest_filled = latest_filled_row[0] if latest_filled_row and latest_filled_row[0] else None

    return filled_qty, has_entry_order, oldest_created, latest_filled


def _child_offset_ok(child_bot_id, parent_direction, conn, min_qty=MIN_HEDGE_QTY,
                     child_step=None, parent_cycle_id=None, engage_timeout=None, now=None):
    """
    Returns True if the hedge child holds an offsetting open position.

    Reads the child's effective hedge position from `bot_orders` (same data
    source as the signal path's saturation check) instead of `trades.open_qty`
    which only reflects credited fills. A proper hedge must be opposite the parent:
      parent LONG  -> child SHORT  (negative child direction)
      parent SHORT -> child LONG   (positive child direction)

    Implements a grace window: if the child has an entry order placed but not yet
    filled, we allow up to `engage_timeout` seconds before considering it a failure.
    """
    if not child_bot_id:
        return False, "no_child_bot_id"

    now = now if now is not None else time.time()
    engage_timeout = engage_timeout or HEDGE_ENGAGE_TIMEOUT_SECONDS

    # Get child bot direction and status
    row = conn.execute(
        "SELECT direction, status FROM bots WHERE id = ?",
        (child_bot_id,)
    ).fetchone()
    if not row:
        return False, "child_not_found"
    child_dir, child_status = row[0], (row[1] or "").upper()
    if child_status in ("REQUIRE_MANUAL_PROOF",):
        return False, "child_require_proof"

    # Compute hedge qty from bot_orders (signal's data source for grace window detection)
    filled_qty, has_entry_order, oldest_created, latest_filled = _child_hedge_qty_from_orders(
        child_bot_id, child_step, parent_cycle_id, conn
    )

    # Offsetting sign check
    if not ((child_dir == "SHORT") != (parent_direction.upper() == "SHORT")):
        return False, "wrong_direction"

    # Engagement check: actual FILLED position >= min_qty
    # This matches the signal's saturation: if signal says saturated (child_step_qty >= parent_target_qty),
    # then filled_qty >= min_qty because parent_target_qty >= step_qty >= min_qty.
    # Pending orders alone don't count as engaged - they trigger the grace window.
    if filled_qty >= min_qty:
        return True, "engaged"

    # No filled position yet — check if we're within the grace window
    if has_entry_order:
        # Grace window: if there's an entry order placed, check its age
        # Prefer filled_at if available (order was filled), else created_at
        reference_ts = latest_filled or oldest_created
        if reference_ts and (now - reference_ts) < engage_timeout:
            return False, "within_grace_window"  # not a failure yet, just wait
        # Grace window expired
        return False, "grace_window_expired"

    # No entry order at all — this is a genuine failure (no attempt made)
    return False, "no_entry_order"


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
        child_step = cfg.get("child_step")
        parent_cycle_id = cfg.get("parent_cycle_id")
        engaged, reason = _child_offset_ok(
            child_bot_id, parent_direction, conn,
            min_qty=min_qty,
            child_step=child_step,
            parent_cycle_id=parent_cycle_id,
            engage_timeout=engage_timeout,
            now=now
        )
        if engaged:
            result["engaged"] = True
            result["reason"] = "engaged"
            # Still need to check escalation even when engaged (if parent was already frozen)
        else:
            result["reason"] = reason
            # If we get here, the hedge has NOT engaged (or child unconfigured).
            # Check if we should freeze or if we're within the grace window.
            # Only freeze if:
            #   - child not configured (no_hedge_child_bot_id)
            #   - child has wrong direction
            #   - child in REQUIRE_MANUAL_PROOF
            #   - grace window expired
            #   - no entry order at all (no attempt made)
            freeze_reasons = {
                "no_hedge_child_bot_id",
                "wrong_direction",
                "child_require_proof",
                "child_not_found",
                "grace_window_expired",
                "no_entry_order",
            }
            if result["reason"] in freeze_reasons:
                result["freeze_parent"] = True
            # "within_grace_window" => do NOT freeze, wait for fill

    # 3. Escalation: if this would be >=2nd distinct parent frozen in window,
    #    engine-wide halt. (Runs regardless of freeze reason)
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