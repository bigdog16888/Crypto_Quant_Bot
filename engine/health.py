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
    per pair: drift_detected (tier-1: floor..now ledger net vs exchange),
              ledger_net / ledger_imbalance / ledger_diff_qty / ledger_diff_usd
              (tier-2: FULL-HISTORY exchange_fills net vs exchange physical)
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
    conn = None
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
            result["last_act_str"] = f"{last_h[0]} {last_h[1]} @ {last_h[2]:.4f}"

        if exchange_instance is not None:
            try:
                bal = exchange_instance.fetch_balance()
                if bal:
                    total_balance = 0.0
                    free_balance = 0.0
                    # fetch_balance returns {'total': {asset: amount}}
                    totals = bal.get('total', {})
                    for asset, amount in totals.items():
                        total = float(amount or 0)
                        free = total  # Binance futures returns total balance, free ≈ total for spot-like assets
                        if asset in ('USDT', 'USDC', 'USD', 'BUSD', 'FDUSD'):
                            total_balance += total
                            free_balance += free
                    result["futures_balance"] = free_balance
                    result["total_equity"] = total_balance
            except Exception:
                pass

        # Assets breakdown
        try:
            if exchange_instance is not None:
                bal = exchange_instance.fetch_balance()
                assets = []
                if bal:
                    for asset, info in bal.items():
                        free = float(info.get("free", 0) or 0)
                        if free > 1e-8:
                            assets.append({"asset": asset, "free": free})
                result["assets_breakdown"] = assets
        except Exception as e:
            logger.warning(f"[health] header_metrics error: {e}")
    finally:
        if conn:
            conn.close()
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
    from engine.position_ledger import compute_bot_position

    netting: Dict[str, Any] = {}
    worst_gap = 0.0
    mismatch_count = 0
    orphan_positions: List[Dict] = []

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        rows = conn.execute(
            """SELECT b.id, b.name, b.pair, b.direction, t.open_qty, t.avg_entry_price, t.cycle_id
               FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
               WHERE b.is_active = 1 OR (t.open_qty IS NOT NULL AND abs(t.open_qty) > 0.0001)"""
        ).fetchall()

        # Also get cycles per pair for exact-cycle compute_pair_position
        # Only include bots that are IN_TRADE or have open_qty > 0 to avoid stale cycle_ids from Scanning bots
        cycle_rows = conn.execute(
            """SELECT b.id, t.cycle_id
               FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
               WHERE b.is_active = 1 AND (b.status = 'IN_TRADE' OR (t.open_qty IS NOT NULL AND abs(t.open_qty) > 0.0001)) AND t.total_invested > 0.01"""
        ).fetchall()

        # Map pair -> current cycle (use max cycle for that pair)
        pair_cycle_map = {}
        for bot_id, cycle_id in cycle_rows:
            if cycle_id:
                bot_row = conn.execute("SELECT pair, normalized_pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
                if bot_row:
                    p_key = norm_fn(bot_row[0] or bot_row[1] or '')
                    if p_key and (p_key not in pair_cycle_map or cycle_id > pair_cycle_map[p_key]):
                        pair_cycle_map[p_key] = cycle_id

        canonical_pairs: Dict[str, str] = {}
        pair_bot_map: Dict[str, List[Dict]] = {}
        for bot_id, bot_name, pair, direction, open_qty, avg_price, cycle_id in rows:
            p_key = norm_fn(pair)
            canonical_pairs.setdefault(p_key, pair)
            pair_bot_map.setdefault(p_key, []).append(dict(
                bot_id=bot_id, name=bot_name, direction=direction,
                open_qty=float(open_qty or 0), avg_price=float(avg_price or 0),
            ))

        conn.close()

        # PRIMARY: compute_pair_position (from immutable exchange_fills log)
        # CROSS-CHECK: get_pair_virtual_net (from trades table - legacy)
        # Use per-bot auto-detected cycle_floor (same logic as recompute_invested_from_orders)
        primary_nets: Dict[str, float] = {}
        ledger_nets: Dict[str, float] = {}  # tier-2: full-history net per pair
        crosscheck_nets: Dict[str, float] = {}
        for p_key, canon_pair in canonical_pairs.items():
            # Get per-bot auto-detected cycle_floor using exchange_fills (authoritative ledger)
            # NOT bot_orders (which may have phantom fills with filled_at=0)
            bot_floors = {}
            bot_has_fills = {}  # Track which bots have exchange_fills for tier-2
            target_cycle = 0
            for bot_info in pair_bot_map.get(p_key, []):
                bot_id = bot_info["bot_id"]
                # Check if bot has any exchange_fills (for tier-2 full-history ledger)
                try:
                    conn2 = sqlite3.connect(db_path, timeout=10)
                    cursor = conn2.cursor()
                    row_fills = cursor.execute(
                        "SELECT COUNT(*) FROM exchange_fills WHERE bot_id = ?", (bot_id,)
                    ).fetchone()
                    bot_has_fills[bot_id] = (row_fills and row_fills[0] > 0)
                    # Only compute floor for bots that are actively trading (IN_TRADE or open_qty > 0)
                    # Scanning bots with open_qty=0 should not contribute to tier-1 netting
                    bot_status = bot_info.get("status", "Scanning")
                    open_qty = bot_info.get("open_qty", 0.0)
                    is_active_trading = (bot_status == 'IN_TRADE' or abs(open_qty) > 0.0001)

                    if is_active_trading:
                        row_trade = cursor.execute(
                            "SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)
                        ).fetchone()
                        if row_trade and row_trade[0]:
                            target_cycle = max(target_cycle, row_trade[0])
                            # Auto-detect cycle_floor from exchange_fills (same logic as recompute but on immutable ledger)
                            row_bot = cursor.execute("SELECT direction FROM bots WHERE id = ?", (bot_id,)).fetchone()
                            bot_dir = row_bot[0].upper() if row_bot else 'LONG'
                            bot_side = bot_dir if bot_dir in ('LONG', 'SHORT') else 'LONG'
                            # For SHORT bots: SELL = entry (opens position), BUY = exit (closes position)
                            # For LONG bots: BUY = entry, SELL = exit
                            if bot_dir == 'SHORT':
                                entry_case = "CASE WHEN side = 'SELL' THEN qty ELSE 0.0 END"
                                exit_case = "CASE WHEN side = 'BUY' THEN qty ELSE 0.0 END"
                            else:
                                entry_case = "CASE WHEN side = 'BUY' THEN qty ELSE 0.0 END"
                                exit_case = "CASE WHEN side = 'SELL' THEN qty ELSE 0.0 END"
                            cursor.execute(f"""
                                SELECT cycle_id,
                                       SUM({entry_case}) AS entry_qty,
                                       SUM({exit_case}) AS exit_qty
                                FROM exchange_fills
                                WHERE bot_id = ?
                                  AND cycle_id < ?
                                  AND cycle_id IS NOT NULL
                                GROUP BY cycle_id
                                HAVING (entry_qty - exit_qty) > 1e-6
                                ORDER BY cycle_id ASC
                                LIMIT 1
                            """, (bot_id, target_cycle))
                            row_floor = cursor.fetchone()
                            bot_floors[bot_id] = row_floor[0] if row_floor else target_cycle
                        else:
                            bot_floors[bot_id] = 0
                    else:
                        # Scanning bots with open_qty=0 don't contribute to tier-1 netting
                        bot_floors[bot_id] = 0
                    conn2.close()
                except Exception:
                    bot_floors[bot_id] = 0
                    bot_has_fills[bot_id] = False

            # Compute using per-bot floors
            # TWO-TIER CHECK:
            #   tier-1 (drift)  = net from auto-detected cycle_floor onward for ACTIVE bots only (current cycle window)
            #   tier-2 (ledger) = net from cycle 0 onward for ALL bots with exchange_fills
            # Both calls now use an explicit connection bound to THIS db_path.
            # The old conn=None fallback (get_connection()) silently bound every
            # caller to the live DB instead of the db_path passed in.
            conn_pair = None
            try:
                if bot_floors:
                    total_net = 0.0
                    total_net_full = 0.0
                    conn_pair = sqlite3.connect(db_path, timeout=10)
                    has_active_bot = False
                    for bot_info in pair_bot_map.get(p_key, []):
                        bot_id = bot_info["bot_id"]
                        floor = bot_floors.get(bot_id, 0)
                        # Only use cycle_ceiling for bots that are actively trading (IN_TRADE or open_qty > 0)
                        # Scanning bots with stale cycle_id should not contribute to tier-1 netting
                        bot_status = bot_info.get("status", "Scanning")
                        open_qty = bot_info.get("open_qty", 0.0)
                        use_ceiling = (bot_status == 'IN_TRADE' or abs(open_qty) > 0.0001)
                        if use_ceiling:
                            has_active_bot = True
                        ceiling = target_cycle if use_ceiling else None
                        # Tier-1 (drift) only counts bots that HOLD A LIVE POSITION
                        # (IN_TRADE or open_qty > tol). Idle bots (Scanning / hedge_standby /
                        # STOPPED at step 0 with open_qty ~ 0) carry only migration-era
                        # exchange_fills residue — their full-history net must NOT inflate
                        # the live tier-1 drift. Zeroing the residual phantom 52.81 SOL /
                        # 0.025 BTC drift introduced when re-activation re-included idle bots.
                        if use_ceiling:
                            bp = compute_bot_position(bot_id, conn=conn_pair, cycle_floor=floor, cycle_ceiling=ceiling)
                            total_net += bp.net_qty
                        # Tier-2: sum full history for ALL bots that have exchange_fills
                        if bot_has_fills.get(bot_id, False):
                            bp_full = compute_bot_position(bot_id, conn=conn_pair, cycle_floor=0, cycle_ceiling=None)
                            total_net_full += bp_full.net_qty
                    # Tier-1 (drift): only pairs with active trading bots should have non-zero primary_net
                    primary_nets[p_key] = total_net if has_active_bot else 0.0
                    ledger_nets[p_key] = total_net_full
                else:
                    primary_nets[p_key] = 0.0
                    ledger_nets[p_key] = 0.0
            except Exception as e:
                logger.warning(f"[NETTING] compute_pair_position failed for {p_key}: {e}")
                primary_nets[p_key] = 0.0
                ledger_nets[p_key] = 0.0
            finally:
                if conn_pair is not None:
                    try:
                        conn_pair.close()
                    except Exception:
                        pass

            # Cross-check: legacy virtual_net (for comparison/logging)
            try:
                crosscheck_nets[p_key] = get_pair_virtual_net(canon_pair)
            except Exception:
                crosscheck_nets[p_key] = 0.0

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
        # Dormant-bots gate (approved spec): a pair is "all-dormant" when EVERY bot
        # on the pair has is_active=0 AND open_qty ~ 0. Pairs that are all-dormant
        # with a flat exchange position carry only historical residue — tier-2
        # ledger_imbalance on them is informational, never a MISMATCH escalation.
        # Pairs with any active bot (is_active=1) or open_qty > 0 stay strict.
        pair_all_dormant: Dict[str, bool] = {}
        try:
            conn_d = sqlite3.connect(db_path, timeout=10)
            for bot_pair, is_active, open_qty in conn_d.execute(
                """SELECT b.pair, b.is_active, COALESCE(t.open_qty, 0.0)
                   FROM bots b LEFT JOIN trades t ON b.id = t.bot_id"""
            ):
                pk = norm_fn(bot_pair or '')
                if not pk:
                    continue
                dormant_bot = (int(is_active or 0) == 0) and (abs(float(open_qty or 0.0)) <= 0.0001)
                pair_all_dormant[pk] = pair_all_dormant.get(pk, True) and dormant_bot
            conn_d.close()
        except Exception as e:
            logger.warning(f"[health] dormant-pair scan failed (non-fatal): {e}")

        for p in sorted(set(primary_nets) | set(physical_nets)):
            # Primary net used for drift/orphan detection
            p_net = primary_nets.get(p, 0.0)
            # Cross-check for logging
            c_net = crosscheck_nets.get(p, 0.0)
            ph_net = physical_nets.get(p, 0.0)
            l_net = ledger_nets.get(p, 0.0)  # tier-2: full-history ledger net

            # Cross-check logging - compare primary vs legacy
            if abs(p_net - c_net) > 0.0001:
                logger.warning(f"[NETTING-CROSSCHECK] Pair {p}: primary={p_net:.6f} legacy={c_net:.6f} DIFF={abs(p_net - c_net):.6f}")

            try:
                for w in get_manual_whitelists(p):
                    adj = float(w["qty"])
                    ph_net -= adj if w["side"] == "LONG" else -adj
            except Exception:
                pass

            ref_price = ref_prices.get(p, 1.0)
            diff_qty = round(abs(p_net - ph_net), 8)
            diff_usd = diff_qty * ref_price
            if diff_usd > worst_gap:
                worst_gap = diff_usd

            # Tier-2: full-history ledger imbalance vs exchange physical position
            ledger_diff_qty = round(abs(l_net - ph_net), 8)
            ledger_diff_usd = ledger_diff_qty * ref_price
            # Dormant-bots gate (approved spec): suppress ledger_imbalance only when
            # ALL bots on the pair are is_active=0 AND flat (open_qty ~ 0) and the
            # exchange physical position is flat. Active pairs (any bot is_active=1
            # or open_qty > 0) keep strict tier-2 mismatch checks.
            is_dormant_pair = pair_all_dormant.get(p, False)
            if is_dormant_pair and abs(ph_net) < tol:
                ledger_imbalance = False
            else:
                ledger_imbalance = (ledger_diff_qty > tol or ledger_diff_usd > 5.0) and not startup_suppression

            # Tier-1 drift detection uses PRIMARY (position_ledger, floor..now window)
            drift = (diff_qty > tol or diff_usd > 5.0) and not startup_suppression
            if drift:
                mismatch_count += 1

            netting[p] = dict(
                pair=p, primary_net=p_net, crosscheck_net=c_net, physical_net=ph_net,
                ledger_net=l_net, ledger_imbalance=ledger_imbalance,
                ledger_diff_qty=ledger_diff_qty, ledger_diff_usd=ledger_diff_usd,
                diff_qty=diff_qty, diff_usd=diff_usd,
                drift_detected=drift, ref_price=ref_price,
                tolerance=tol, bots=pair_bot_map.get(p, []),
                dormant_pair=is_dormant_pair,
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


# ---------------------------------------------------------------------------
# Order Health
# ---------------------------------------------------------------------------

def _compute_order_health(
    db_path: str,
    open_exchange_orders: List[Dict],
    bot_df_rows: List[Dict],
    startup_suppression: bool,
) -> Dict[str, Any]:
    """Check order consistency between DB and exchange."""
    result = dict(
        status_color="green",
        message="All orders synced",
        bot_statuses=[],
        dust_bots=[],
    )

    try:
        # Build map of exchange orders by client_order_id
        ex_orders = {}
        for o in open_exchange_orders:
            cid = o.get("clientOrderId") or o.get("info", {}).get("clientOrderId")
            if cid:
                ex_orders[str(cid)] = o

        for bot in bot_df_rows:
            bot_id = bot["id"]
            name = bot["name"]
            status = bot["status"]
            total_inv = float(bot.get("total_invested") or 0)
            step = bot.get("current_step")
            cycle_phase = bot.get("cycle_phase")
            bot_type = bot.get("bot_type")
            parent_id = bot.get("parent_bot_id")
            config = bot.get("config")

            bot_info = {
                "id": bot_id,
                "name": name,
                "status": status,
                "total_invested": total_inv,
                "step": step,
                "cycle_phase": cycle_phase,
                "bot_type": bot_type,
                "parent_bot_id": parent_id,
            }
            result["bot_statuses"].append(bot_info)

            # ── Hedge child with inactive parent + old open order check ──
            if bot_type == "hedge_child" and parent_id:
                parent_bot = next((b for b in bot_df_rows if b["id"] == parent_id), None)
                if parent_bot and parent_bot.get("status") in ("Scanning", "IDLE") and total_inv > 0:
                    # Child has investment but parent is not actively trading - check for stale open orders
                    try:
                        conn = sqlite3.connect(db_path, timeout=5)
                        old_orders = conn.execute(
                            """SELECT COUNT(*) FROM bot_orders 
                               WHERE bot_id = ? AND status = 'open' AND created_at < ?""",
                            (bot_id, int(time.time()) - 60)
                        ).fetchone()[0]
                        conn.close()
                        if old_orders > 0:
                            result["status_color"] = "yellow"
                            result["message"] = f"Hedge child {name} has {old_orders} stale open order(s) with inactive parent"
                    except Exception:
                        pass

    except Exception as e:
        logger.warning(f"[health] order_health error: {e}")
        result["status_color"] = "yellow"
        result["message"] = f"Order health check partial: {e}"

    return result


def _compute_critical_bot_states(db_path: str) -> Dict[str, List[str]]:
    """Detect bots in engine-blocking states: stuck cascade, manual proof, dust no-exit."""
    result = {"stuck_cascade_bots": [], "manual_proof_bots": []}
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        stuck_statuses = ('pending_close', 'pending_hedge_close', 'FLATTENING', 'pending_flatten')
        rows = conn.execute(
            """SELECT b.id, b.name, b.status, b.cascade_started_at, t.basket_start_time
               FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
               WHERE b.is_active = 1 AND (b.status IN ({})
               OR b.status = 'REQUIRE_MANUAL_PROOF')""".format(",".join("?"*len(stuck_statuses))),
            stuck_statuses
        ).fetchall()

        for bot_id, name, status, cascade_started_at, basket_ts in rows:
            if status == 'REQUIRE_MANUAL_PROOF':
                result["manual_proof_bots"].append(name)
            elif status in stuck_statuses:
                start_time = cascade_started_at if (cascade_started_at and cascade_started_at > 0) else basket_ts
                stuck_duration = int(time.time()) - int(start_time or 0)
                if stuck_duration > 300:  # CASCADE_TIMEOUT
                    result["stuck_cascade_bots"].append(name)
        conn.close()
    except Exception as e:
        logger.warning(f"[health] critical_bot_states error: {e}")
    return result


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
    # Tier-1 (drift): only mismatch_count matters for live exchange parity
    # Tier-2 (ledger): ledger_imbalance is advisory only, does not affect top status
    if suppression:
        system_status = "STARTING"
        tier1_status = "STARTING"
        tier2_status = "STARTING"
    elif stuck_cascade_bots or manual_proof_bots or dust_bots:
        system_status = "CRITICAL"
        tier1_status = "CRITICAL"
        tier2_status = "CRITICAL"
    elif mismatch_count > 0:
        # Tier-1 drift detected - live exchange parity issue
        system_status = "MISMATCH"
        tier1_status = "MISMATCH"
        tier2_status = "MISMATCH" if any(v.get("ledger_imbalance") for v in netting.values()) else "HEALTHY"
    elif any(v.get("ledger_imbalance") for v in netting.values()):
        # Tier-2 ledger imbalance only - exchange parity is HEALTHY
        system_status = "HEALTHY"
        tier1_status = "HEALTHY"
        tier2_status = "LEDGER_ADVISORY"
    elif order_health.get("status_color") == "red":
        system_status = "WARNING"
        tier1_status = "WARNING"
        tier2_status = "WARNING"
    else:
        system_status = "HEALTHY"
        tier1_status = "HEALTHY"
        tier2_status = "HEALTHY"

    return {
        "timestamp": now,
        "startup_suppression": suppression,
        "startup_remaining_s": remaining,
        "engine_started_at": engine_started_at,
        "system_status": system_status,
        "tier1_status": tier1_status,
        "tier2_status": tier2_status,
        "worst_gap_usd": worst_gap,
        "mismatched_pair_count": mismatch_count,
        "netting_status_per_pair": netting,
        "order_health": order_health,
        "header_metrics": header,
        "orphan_positions": orphans,
        "stuck_cascade_bots": stuck_cascade_bots,
        "manual_proof_bots": manual_proof_bots,
    }


# ---------------------------------------------------------------------------
# Public API: TTL-cached wrapper (backward compatibility)
# ---------------------------------------------------------------------------

_health_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_HEALTH_TTL_SECONDS = 5.0


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
    TTL-cached wrapper around compute_system_health().

    Parameters
    ----------
    force_refresh : bool
        If True, bypass cache and recompute immediately.

    Returns
    -------
    Dict with same structure as compute_system_health().
    """
    now = time.time()
    if force_refresh:
        # Invalidate module-level cache on force_refresh
        _health_cache["data"] = None
        _health_cache["ts"] = 0.0
    if not force_refresh:
        cached = _health_cache.get("data")
        if cached is not None and (now - _health_cache.get("ts", 0)) < _HEALTH_TTL_SECONDS:
            return cached

    result = compute_system_health(
        db_path=db_path,
        exchange_instance=exchange_instance,
        norm_fn=norm_fn,
        qty_tolerance_fn=qty_tolerance_fn,
        open_exchange_orders=open_exchange_orders,
        bot_df_rows=bot_df_rows,
    )

    _health_cache["data"] = result
    _health_cache["ts"] = now
    return result


def invalidate_health_cache() -> None:
    """Force next get_system_health() call to recompute."""
    _health_cache["data"] = None
    _health_cache["ts"] = 0.0