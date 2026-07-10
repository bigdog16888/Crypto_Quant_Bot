"""
engine/health.py  -  Single Authoritative System Health Computation
====================================================================
compute_system_health() is the ONE place all health state is decided.
The UI (monitor.py) calls get_system_health() once per refresh cycle
and passes health_data to every fragment.  No fragment may make its own
independent netting / order-health computation.

Keys returned
-------------
  timestamp              float  - unix time of this computation
  startup_suppression    bool   - True if within 120 s of ENGINE_STARTED_AT
  startup_remaining_s    float  - seconds left in grace period
  engine_started_at      float  - ENGINE_STARTED_AT value (0 if missing)
  system_status          str    - STARTING | HEALTHY | WARNING | MISMATCH | CRITICAL
  worst_gap_usd          float
  mismatched_pair_count  int
  netting_status_per_pair dict  - keyed by normalised pair string
  order_health           dict   - {status_color, message, bot_statuses}
  header_metrics         dict   - all header tile values
  orphan_positions       list   - exchange positions with no bot ownership
  stuck_cascade_bots     list   - bot names currently in stuck cascade (pending_flatten etc.)
                                  that have exceeded GTR.CASCADE_TIMEOUT
  manual_proof_bots      list   - bot names locked to REQUIRE_MANUAL_PROOF status
                                  requiring human resolution before engine can proceed
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS: float = 120.0  # suppress health alerts for this many seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_engine_started_at(db_path: str) -> float:
    """Read ENGINE_STARTED_AT from system_equity. Returns 0.0 if absent."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute(
            "SELECT value FROM system_equity WHERE key='ENGINE_STARTED_AT'"
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def _compute_header_metrics(db_path: str, exchange_instance) -> Dict[str, Any]:
    """Aggregate all header tile values from DB + exchange (best-effort)."""
    result: Dict[str, Any] = dict(
        total_equity=0.0, futures_balance=0.0, global_pnl_usd=0.0,
        total_invested_db=0.0, active_count=0, bots_in_trade=0,
        scanning_count=0, open_qty_notional=0.0, assets_breakdown=[],
        adoptions_today=0, last_act_str="NO RECENT ACTIVITY",
    )
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
        result["active_count"] = int(cur.fetchone()[0] or 0)

        cur.execute(
            "SELECT COUNT(*) FROM trades t JOIN bots b ON b.id=t.bot_id "
            "WHERE b.is_active=1 AND t.total_invested > 0.01"
        )
        result["bots_in_trade"] = int(cur.fetchone()[0] or 0)
        result["scanning_count"] = max(0, result["active_count"] - result["bots_in_trade"])

        cur.execute("SELECT SUM(total_invested) FROM trades WHERE total_invested > 0")
        r = cur.fetchone()
        result["total_invested_db"] = float(r[0] or 0.0)

        cur.execute(
            "SELECT COALESCE(SUM(t.open_qty * t.avg_entry_price), 0) "
            "FROM trades t JOIN bots b ON b.id=t.bot_id "
            "WHERE b.is_active=1 AND t.open_qty > 1e-8 AND t.avg_entry_price > 0"
        )
        result["open_qty_notional"] = float(cur.fetchone()[0] or 0.0)

        cur.execute(
            "SELECT COUNT(*) FROM reconciliation_logs "
            "WHERE action LIKE '%ADOPTION%' AND timestamp > ?",
            (int(time.time()) - 86400,)
        )
        result["adoptions_today"] = int(cur.fetchone()[0] or 0)

        cur.execute(
            "SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1"
        )
        last_h = cur.fetchone()
        if last_h:
            result["last_act_str"] = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}"

        cur.execute(
            "SELECT t.total_invested, t.avg_entry_price, b.pair, b.direction "
            "FROM trades t JOIN bots b ON t.bot_id = b.id "
            "WHERE t.total_invested > 0 AND b.is_active = 1"
        )
        active_trades = cur.fetchall()
        conn.close()

        price_map: Dict[str, float] = {}
        if active_trades and exchange_instance is not None:
            for sym in set(t[2] for t in active_trades):
                try:
                    px = exchange_instance.get_last_price(sym)
                    if px:
                        price_map[sym] = float(px)
                except Exception:
                    pass

        pnl = 0.0
        for inv, entry, pair, direction in active_trades:
            curr = price_map.get(pair, 0.0)
            if curr > 0 and float(entry or 0) > 0.0001:
                if direction == "LONG":
                    pnl += (curr - entry) / entry * inv
                else:
                    pnl += (entry - curr) / entry * inv
        result["global_pnl_usd"] = pnl

        futures_balance = 0.0
        assets: List[Dict] = []
        if exchange_instance is not None:
            try:
                fut = exchange_instance.fetch_balance()
                if fut and "total" in fut:
                    for asset, amount in fut["total"].items():
                        if amount and amount > 0:
                            assets.append(dict(
                                Type="Futures", Asset=asset, Balance=amount,
                                Unrealized_PnL=0.0, Equity=amount,
                            ))
                            if asset in ("USDT", "USDC", "USD", "BUSD"):
                                futures_balance += amount
            except Exception:
                pass
        result["futures_balance"] = futures_balance
        result["total_equity"] = futures_balance + pnl
        result["assets_breakdown"] = assets

    except Exception as e:
        logger.warning(f"[health] header_metrics error: {e}")
    return result


def _compute_netting_status(
    db_path: str,
    exchange_instance,
    startup_suppression: bool,
    norm_fn: Callable[[str], str],
    qty_tolerance_fn: Callable[[], float],
) -> tuple:
    """Returns (netting_per_pair, worst_gap_usd, mismatch_count, orphan_positions)."""
    from engine.database import get_pair_virtual_net, get_manual_whitelists

    netting: Dict[str, Any] = {}
    worst_gap = 0.0
    mismatch_count = 0
    orphan_positions: List[Dict] = []

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        rows = conn.execute(
            """SELECT b.id, b.name, b.pair, b.direction, t.open_qty, t.avg_entry_price
               FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
               WHERE b.is_active = 1"""
        ).fetchall()
        conn.close()

        canonical_pairs: Dict[str, str] = {}
        pair_bot_map: Dict[str, List[Dict]] = {}
        for bot_id, bot_name, pair, direction, open_qty, avg_price in rows:
            p_key = norm_fn(pair)
            canonical_pairs.setdefault(p_key, pair)
            pair_bot_map.setdefault(p_key, []).append(dict(
                bot_id=bot_id, name=bot_name, direction=direction,
                open_qty=float(open_qty or 0), avg_price=float(avg_price or 0),
            ))

        virtual_nets: Dict[str, float] = {}
        for p_key, canon_pair in canonical_pairs.items():
            try:
                virtual_nets[p_key] = get_pair_virtual_net(canon_pair)
            except Exception:
                virtual_nets[p_key] = 0.0

        physical_nets: Dict[str, float] = {}
        ref_prices: Dict[str, float] = {}
        if exchange_instance is not None:
            try:
                for pos in (exchange_instance.fetch_positions() or []):
                    amt = float(pos.get("contracts", 0) or pos.get("size", 0) or 0)
                    if abs(amt) < 1e-12:
                        continue
                    p_key = norm_fn(pos.get("symbol", ""))
                    physical_nets[p_key] = physical_nets.get(p_key, 0.0) + amt
                    ep = float(pos.get("entryPrice", 0) or 0)
                    if ep > 0 and p_key not in ref_prices:
                        ref_prices[p_key] = ep
            except Exception as ex:
                logger.warning(f"[health] fetch_positions failed: {ex}")

        tol = qty_tolerance_fn()
        for p in sorted(set(virtual_nets) | set(physical_nets)):
            v_net = virtual_nets.get(p, 0.0)
            ph_net = physical_nets.get(p, 0.0)
            try:
                for w in get_manual_whitelists(p):
                    adj = float(w["qty"])
                    ph_net -= adj if w["side"] == "LONG" else -adj
            except Exception:
                pass

            ref_price = ref_prices.get(p, 1.0)
            diff_qty = round(abs(v_net - ph_net), 8)
            diff_usd = diff_qty * ref_price
            if diff_usd > worst_gap:
                worst_gap = diff_usd

            # Drift is detected if the gap exceeds the quantity tolerance OR if the USD value of the gap exceeds $5.00
            drift = (diff_qty > tol or diff_usd > 5.0) and not startup_suppression
            if drift:
                mismatch_count += 1

            netting[p] = dict(
                pair=p, virtual_net=v_net, physical_net=ph_net,
                diff_qty=diff_qty, diff_usd=diff_usd,
                drift_detected=drift, ref_price=ref_price,
                tolerance=tol, bots=pair_bot_map.get(p, []),
            )

            bot_qty = sum(abs(b["open_qty"]) for b in pair_bot_map.get(p, []))
            if abs(ph_net) > tol and bot_qty < tol and not startup_suppression:
                orphan_positions.append(dict(
                    pair=p, exchange_net=ph_net,
                    ref_price=ref_price, notional_usd=abs(ph_net) * ref_price,
                ))

    except Exception as e:
        logger.error(f"[health] netting computation error: {e}")
    return netting, worst_gap, mismatch_count, orphan_positions


def _compute_order_health(
    db_path: str,
    open_exchange_orders: List[Dict],
    bot_df_rows: List[Dict],
    startup_suppression: bool,
) -> Dict[str, Any]:
    """Per-bot order health + aggregate status."""
    physical_counts: Dict[int, int] = {}
    for o in open_exchange_orders:
        cid = str(o.get("clientOrderId") or "")
        if cid.startswith("CQB_"):
            try:
                bid = int(cid.split("_")[1])
                physical_counts[bid] = physical_counts.get(bid, 0) + 1
            except Exception:
                pass

    missing: List[str] = []
    no_exit: List[str] = []
    partial: List[str] = []
    margin_held: List[str] = []
    dust: List[str] = []
    bot_statuses: Dict[int, Dict] = {}

    for row in bot_df_rows:
        bid = int(row["id"])
        inv = float(row.get("total_invested") or 0)
        c_step = int(row.get("current_step") or 0)
        phase = str(row.get("cycle_phase", "IDLE")).upper()
        actual_ph = physical_counts.get(bid, 0)
        b_status = str(row.get("status", "")).upper()

        if "SCANNING" in b_status and inv <= 0.01:
            status_str = "SCANNING"
        elif inv > 0.01:
            status_str = f"IN TRADE | Step {c_step}"
        else:
            status_str = b_status or "IDLE"

        bot_statuses[bid] = dict(status=status_str, active_orders=actual_ph)

        if ("EXITING" in b_status) or ("SCANNING" in b_status and inv <= 0.01):
            continue

        if phase == "STUCK_DUST_NO_EXIT":
            dust.append(row["name"])
            continue

        # Exempt hedge child bots whose parents are still active from missing order alerts
        if row.get("bot_type") == "hedge_child" and row.get("parent_bot_id"):
            parent_id = int(row["parent_bot_id"])
            parent_row = next((r for r in bot_df_rows if int(r["id"]) == parent_id), None)
            if parent_row:
                parent_inv = float(parent_row.get("total_invested") or 0)
                if parent_inv > 0.01:
                    # Parent is active, so child expects 0 open orders by design.
                    continue

        if actual_ph == 0 and inv > 0.01 and phase not in ("CARRY_PENDING",):
            if startup_suppression:
                continue
            last_ts = 0.0
            try:
                c = sqlite3.connect(db_path, timeout=5)
                r = c.execute(
                    "SELECT MAX(created_at) FROM bot_orders WHERE bot_id=?", (bid,)
                ).fetchone()
                c.close()
                if r and r[0]:
                    last_ts = float(r[0])
            except Exception:
                pass
            if (time.time() - last_ts) < 60:
                continue
            missing.append(row["name"])
        elif phase == "MARGIN_HELD":
            margin_held.append(row["name"])

    if startup_suppression:
        msg, color = "STARTUP: Health alerts suppressed during engine grace period.", "orange"
    elif dust:
        msg, color = f"STUCK DUST NO EXIT: {', '.join(dust)}", "red"
    elif no_exit:
        msg, color = f"NO EXIT ORDER: {', '.join(no_exit)}", "red"
    elif missing:
        msg, color = f"MISSING CRITICAL ORDERS: {', '.join(missing)}", "red"
    elif margin_held:
        msg, color = (
            f"MARGIN HELD: {', '.join(margin_held)} — "
            "Free margin to allow TP placement."
        ), "orange"
    elif partial:
        msg, color = f"MISSING GRIDS: {', '.join(partial)}", "orange"
    else:
        msg, color = f"ORDERS SYNCED: {len(open_exchange_orders)} active orders.", "green"

    return dict(status_color=color, message=msg, bot_statuses=bot_statuses, dust_bots=dust)


# ---------------------------------------------------------------------------
# GTR Critical State Detector  (improvements #1 and #4)
# ---------------------------------------------------------------------------

_GTR_CASCADE_TIMEOUT: int = 300  # matches GroundTruthReconciler.CASCADE_TIMEOUT
_GTR_CASCADE_STATUSES = (
    "pending_close", "pending_hedge_close", "FLATTENING", "pending_flatten"
)


def _compute_critical_bot_states(db_path: str) -> Dict[str, List[str]]:
    """
    Query the DB for bots that are in a GTR-critical state.

    Returns a dict with two lists of bot *names*:
      stuck_cascade_bots  – stuck in a cascade status longer than CASCADE_TIMEOUT
      manual_proof_bots   – locked to REQUIRE_MANUAL_PROOF (human action required)

    This does NOT call the exchange; it reads only the bots table so it is fast
    and safe to call on every health computation cycle.
    """
    stuck: List[str] = []
    proof: List[str] = []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        now = int(time.time())
        # Stuck cascade: status in cascade statuses AND cascade_started_at exceeded timeout
        cascade_rows = conn.execute(
            f"""
            SELECT b.name, b.status, b.cascade_started_at, t.basket_start_time
            FROM bots b
            LEFT JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
              AND b.status IN ({','.join('?' * len(_GTR_CASCADE_STATUSES))})
            """,
            _GTR_CASCADE_STATUSES,
        ).fetchall()
        for name, status, cascade_ts, basket_ts in cascade_rows:
            start = cascade_ts if (cascade_ts and cascade_ts > 0) else (basket_ts or 0)
            if (now - int(start)) > _GTR_CASCADE_TIMEOUT:
                stuck.append(name)

        # Manual proof: any bot locked to REQUIRE_MANUAL_PROOF
        proof_rows = conn.execute(
            "SELECT name FROM bots WHERE is_active = 1 AND status = 'REQUIRE_MANUAL_PROOF'"
        ).fetchall()
        proof = [r[0] for r in proof_rows]
        conn.close()
    except Exception as e:
        logger.warning(f"[health] _compute_critical_bot_states error: {e}")
    return dict(stuck_cascade_bots=stuck, manual_proof_bots=proof)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_system_health(
    db_path: str,
    exchange_instance,
    norm_fn: Callable[[str], str],
    qty_tolerance_fn: Callable[[], float],
    open_exchange_orders: Optional[List[Dict]] = None,
    bot_df_rows: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Compute the single authoritative system health snapshot.

    Parameters
    ----------
    db_path               Absolute path to crypto_bot.db.
    exchange_instance     ExchangeInterface or None (skips live exchange calls).
    norm_fn               Callable(str)->str that normalises a pair/symbol string.
    qty_tolerance_fn      Callable()->float returning the current qty tolerance.
    open_exchange_orders  Pre-fetched open orders list (fetched via exchange if None).
    bot_df_rows           Pre-fetched bot dicts (fetched from DB if None).
    """
    now = time.time()
    engine_started_at = _get_engine_started_at(db_path)
    age = now - engine_started_at if engine_started_at > 0 else 9999.0
    suppression = age < STARTUP_GRACE_SECONDS
    remaining = max(0.0, STARTUP_GRACE_SECONDS - age)

    header = _compute_header_metrics(db_path, exchange_instance)

    netting, worst_gap, mismatch_count, orphans = _compute_netting_status(
        db_path, exchange_instance, suppression, norm_fn, qty_tolerance_fn,
    )

    if open_exchange_orders is None:
        open_exchange_orders = []
        if exchange_instance is not None:
            try:
                open_exchange_orders = exchange_instance.fetch_open_orders(None) or []
            except Exception as e:
                logger.warning(f"[health] fetch_open_orders failed: {e}")

    if bot_df_rows is None:
        bot_df_rows = []
        try:
            c = sqlite3.connect(db_path, timeout=10)
            raw = c.execute(
                """SELECT b.id, b.name, b.status, t.total_invested, t.current_step,
                          t.cycle_phase, b.bot_type, b.parent_bot_id, b.config
                   FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
                   WHERE b.is_active = 1"""
            ).fetchall()
            c.close()
            cols = ["id", "name", "status", "total_invested", "current_step",
                    "cycle_phase", "bot_type", "parent_bot_id", "config"]
            bot_df_rows = [dict(zip(cols, r)) for r in raw]
        except Exception as e:
            logger.warning(f"[health] bot_df_rows fetch error: {e}")

    order_health = _compute_order_health(
        db_path, open_exchange_orders, bot_df_rows, suppression
    )

    # ── Improvement #1 / #4: detect GTR-critical bot states ────────────────
    critical_states = _compute_critical_bot_states(db_path)
    stuck_cascade_bots = critical_states["stuck_cascade_bots"]
    manual_proof_bots  = critical_states["manual_proof_bots"]

    dust_bots: List[str] = order_health.get("dust_bots", [])

    # system_status priority: STARTING > CRITICAL > MISMATCH > WARNING > HEALTHY
    if suppression:
        system_status = "STARTING"
    elif stuck_cascade_bots or manual_proof_bots or dust_bots:
        # Stuck cascade, unresolved manual-proof, or STUCK_DUST_NO_EXIT are all
        # engine-blocking conditions that cannot be auto-healed and require human
        # intervention. All three escalate to CRITICAL. [INV-35]
        system_status = "CRITICAL"
    elif mismatch_count > 0:
        system_status = "MISMATCH"
    elif order_health["status_color"] == "red":
        system_status = "WARNING"
    else:
        system_status = "HEALTHY"

    return dict(
        timestamp=now,
        startup_suppression=suppression,
        startup_remaining_s=remaining,
        engine_started_at=engine_started_at,
        system_status=system_status,
        worst_gap_usd=worst_gap,
        mismatched_pair_count=mismatch_count,
        netting_status_per_pair=netting,
        order_health=order_health,
        header_metrics=header,
        orphan_positions=orphans,
        stuck_cascade_bots=stuck_cascade_bots,
        manual_proof_bots=manual_proof_bots,
        dust_bots=dust_bots,
    )


# ---------------------------------------------------------------------------
# Cached accessor for the UI
# ---------------------------------------------------------------------------

_health_cache: Dict[str, Any] = {}
_CACHE_TTL: float = 10.0


def get_system_health(
    db_path: str,
    exchange_instance,
    norm_fn: Callable[[str], str],
    qty_tolerance_fn: Callable[[], float],
    open_exchange_orders: Optional[List[Dict]] = None,
    bot_df_rows: Optional[List[Dict]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Cached wrapper around compute_system_health().

    force_refresh=True bypasses the TTL (use for the 'Refresh Now' button so
    the operator gets immediate feedback after a manual action, not stale data).
    Returns stale cache data if a fresh computation raises an exception.
    """
    global _health_cache
    db_engine_started_at = _get_engine_started_at(db_path)
    cached_engine_started_at = _health_cache.get("engine_started_at", 0.0)
    if db_engine_started_at != cached_engine_started_at:
        force_refresh = True

    age = time.time() - _health_cache.get("timestamp", 0.0)
    if not force_refresh and age < _CACHE_TTL and _health_cache:
        return _health_cache

    try:
        result = compute_system_health(
            db_path, exchange_instance, norm_fn, qty_tolerance_fn,
            open_exchange_orders, bot_df_rows,
        )
        _health_cache = result
        return result
    except Exception as e:
        logger.error(f"[health] get_system_health failed: {e}")
        if _health_cache:
            return _health_cache  # serve stale rather than crash UI
        return dict(
            timestamp=time.time(), startup_suppression=False,
            startup_remaining_s=0.0, engine_started_at=0.0,
            system_status="UNKNOWN", worst_gap_usd=0.0,
            mismatched_pair_count=0, netting_status_per_pair={},
            order_health=dict(
                status_color="orange",
                message="Health check unavailable.",
                bot_statuses={},
            ),
            header_metrics={}, orphan_positions=[],
        )
