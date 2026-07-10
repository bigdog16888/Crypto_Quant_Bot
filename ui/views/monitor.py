import json
import streamlit as st
import time
import pandas as pd
import plotly.graph_objects as go
import ccxt
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from engine.exchange_interface import ExchangeInterface
from engine.database import (
    get_connection, get_bots_by_order_id, get_unread_notifications, 
    mark_notifications_read, import_position_from_exchange,
    add_manual_whitelist, clear_manual_whitelists_for_pair, get_manual_whitelists
)
from engine.reconciler import StateReconciler, ReconciliationAction
from engine.parity_gates import qty_tolerance as pair_qty_tolerance
from config.settings import config as global_config
from engine.exchange_interface import normalize_symbol as _norm_universal
from engine.health import get_system_health as _get_system_health

# --- Performance Caching Wrappers ---
@st.cache_resource(ttl=3600, show_spinner=False)
def get_exchange_instance(market_type):
    """Singleton provider for ExchangeInterface to reuse connections."""
    return ExchangeInterface(market_type=market_type)


@st.cache_data(ttl=5, show_spinner=False)
def fetch_ohlcv_cached(market_type, symbol, timeframe):
    """
    UI PERFORMANCE BATCHING 🚀
    Reads pre-fetched OHLCV data from the Engine's local JSON cache
    instead of making parallel REST API calls that stall the Dashboard.
    """
    try:
        cache_file = os.path.join(global_config.ROOT_DIR, 'data', 'market_cache.json')
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            # Find the symbol in the cache
            # Cache keys might be normalized (e.g., BTC/USDC)
            if symbol in cache_data and timeframe in cache_data[symbol]:
                return cache_data[symbol][timeframe]
            
        # Fallback to direct fetch if cache is missing or stale
        ex = get_exchange_instance(market_type)
        norm_symbol = symbol
        if market_type == 'future' and ':' not in symbol:
            if symbol.endswith('/USDT'): norm_symbol = f"{symbol}:USDT"
            elif symbol.endswith('/USDC'): norm_symbol = f"{symbol}:USDC"
        
        return ex.fetch_ohlcv(norm_symbol, timeframe=timeframe, limit=100)
    except Exception as e: 
        print(f"OHLCV Error for {symbol}: {e}")
        return []

@st.cache_data(ttl=5, show_spinner=False)
def fetch_positions_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_positions()
    except Exception as e:
        print(f"UI Fetch Error: {e}")
        return []

@st.cache_data(ttl=5, show_spinner=False)
def fetch_open_orders_cached(market_type, symbol):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_open_orders(symbol)
    except Exception as e:
        print(f"Error fetching orders for {symbol}: {e}")
        return []

@st.cache_data(ttl=15, show_spinner=False)
def fetch_balance_cached(market_type):
    try:
        ex = get_exchange_instance(market_type)
        return ex.fetch_balance()
    except Exception: return {}
# ------------------------------------

def _fetch_fresh_monitor_data():
    """Fetches the latest bot and physical state from DB + Exchange for fragments."""
    m_orders = []
    exchange_error = None
    try:
        db_path = global_config.PATHS['DB_FILE']
        with sqlite3.connect(db_path, timeout=10) as conn_fresh:
            # 1. Fetch Bot Strategies (Explicit Aliases for mapping safety)
            query_all = """
                SELECT b.id AS id, b.name AS name, b.pair AS pair, b.direction AS direction, 
                       b.strategy_type AS strategy_type, b.config AS config, t.current_step AS current_step, 
                       t.total_invested AS total_invested, t.avg_entry_price AS avg_entry_price, 
                       t.target_tp_price AS target_tp_price, b.is_active AS is_active, b.status AS status, 
                       b.error AS error, t.basket_start_time AS basket_start_time, 
                       t.cycle_start_time AS cycle_start_time, t.cycle_phase AS cycle_phase, 
                       t.open_qty AS open_qty, b.bot_type AS bot_type, b.parent_bot_id AS parent_bot_id,
                       b.hedge_child_bot_id AS hedge_child_bot_id,
                       (SELECT pb.name FROM bots pb WHERE pb.id = b.parent_bot_id) AS parent_name,
                       (SELECT pb.hedge_trigger_step FROM bots pb WHERE pb.id = b.parent_bot_id) AS parent_hedge_trigger_step
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
                WHERE b.is_active = 1
            """
            df_p = pd.read_sql(query_all, conn_fresh)
            
            # Filter out hedge standby bots that shouldn't be shown
            if not df_p.empty:
                keep_mask = []
                for idx, row in df_p.iterrows():
                    if row.get('bot_type') != 'hedge_child':
                        keep_mask.append(True)
                        continue
                    
                    # It is a hedge child — keep only if active
                    inv = float(row.get('total_invested', 0) or 0)
                    phase = str(row.get('cycle_phase', '')).upper()
                    has_active_trade = (inv > 0.01) or (phase == 'ACTIVE')
                    
                    if has_active_trade:
                        keep_mask.append(True)
                    else:
                        keep_mask.append(False)
                
                df_p = df_p[keep_mask].reset_index(drop=True)
                
                # Add indicator to parent bots
                df_p['name'] = df_p.apply(
                    lambda r: f"{r['name']} 🛡️ Hedge armed" if pd.notna(r.get('hedge_child_bot_id')) and r.get('hedge_child_bot_id') else r['name'],
                    axis=1
                )
            
            # 2. Fetch Physical Positions
            try:
                df_ph = pd.read_sql("SELECT pair, side, size, entry_price, last_checked FROM active_positions", conn_fresh)
            except:
                df_ph = pd.DataFrame()
            
            # 3. Fetch Market Orders (Live from Exchange)
            try:
                m_orders = fetch_open_orders_cached(global_config.MARKET_TYPE, None)
            except Exception as e:
                exchange_error = f"Exchange Order Sync Error: {e}"
            
            # 4. Fetch Hedge Orders
            # ── ARCH NOTE ──────────────────────────────────────────────────────────
            # Hedge orders are physical exchange SHORT positions. They are NOT
            # bounded by wipe_wall_ts (which is a virtual accounting fence for
            # ENTRY fills only). A hedge placed in cycle 5 is still open on the
            # exchange in cycle 37 unless explicitly closed (reset_cleared / auto_closed).
            # Removing the wipe_wall_ts filter here ensures:
            #   1. hedged_bot_ids correctly includes bots with pre-reset hedges
            #   2. hedge_amounts correctly sums all outstanding hedge exposure
            #   3. MISSING CRITICAL ORDERS alert is not falsely fired for hedge child bots
            # ───────────────────────────────────────────────────────────────────────
            query_h = """
                SELECT bo.bot_id, bo.order_type, bo.filled_amount, bo.status, bo.created_at
                FROM bot_orders bo
                WHERE bo.order_type IN ('hedge', 'hedge_tp', 'hedgetp')
                  AND bo.status NOT IN ('canceled', 'cancelled', 'rejected', 'failed',
                                        'reset_cleared', 'auto_closed', 'placing')
                  AND bo.filled_amount > 0
            """
            df_h = pd.read_sql(query_h, conn_fresh)
            
            return df_p, df_ph, m_orders, df_h, exchange_error
    except Exception as e:
        print(f"Fragment Data Fetch Error: {e}")
        return pd.DataFrame(), pd.DataFrame(), [], pd.DataFrame(), str(e)


def _render_header_ui(data):
    # ── Compact 5-tile single-row header ──────────────────────────────────────
    # Tile 5 = live system status pill (STARTING/HEALTHY/WARNING/MISMATCH/CRITICAL)
    # with worst-gap inline when non-zero.  Second bots-count row removed —
    # that info is already visible per-row in the bot table below.
    _STATUS_ICONS = {
        'HEALTHY':  '🟢', 'WARNING': '🟡', 'MISMATCH': '🔴',
        'CRITICAL': '🔴', 'STARTING': '⏳',
    }
    sys_status  = data.get('system_status', 'UNKNOWN')
    worst_gap   = data.get('worst_gap_usd', 0.0)
    icon        = _STATUS_ICONS.get(sys_status, '⚪')
    gap_label   = f"  ·  Gap ${worst_gap:,.2f}" if worst_gap > 0.01 else ""
    status_str  = f"{icon} {sys_status}{gap_label}"

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1: st.metric("💰 Equity",   f"${data['total_equity']:,.2f}")
    with m2: st.metric("🏦 Balance",  f"${data['futures_balance']:,.2f}")
    with m3: st.metric("📈 PnL",      f"${data['global_pnl_usd']:,.2f}")
    with m4: st.metric("💼 Invested", f"${data['total_invested_db']:,.2f}",
                       help="Sum of trades.total_invested across active bots (ledger exposure).")
    with m5: st.metric("⚡ Status",   status_str,
                       help=f"In Trade: {data['bots_in_trade']}/{data['active_count']} | "
                            f"Adoptions(24h): {data['adoptions_today']}")

    if data['assets_breakdown']:
        with st.expander("💰 Detailed Asset Breakdown", expanded=False):
            st.table(pd.DataFrame(data['assets_breakdown']))
    st.divider()

    # Compact status ribbon — last activity only
    st.caption(f"⚡ CORE: ONLINE  |  LAST: {data['last_act_str']}")


@st.fragment(run_every=30)
def _header_metrics_fragment():
    # Display Sync Status within fragment
    st.caption(f"  ⚡ Header Sync: {time.strftime('%H:%M:%S')}")

    auto_refresh = st.session_state.get("auto_refresh_toggle", True)
    wizard_active = any(bool(st.session_state.get(k)) for k in st.session_state
                        if k.startswith(("forensic_trades_", "adopt_force_sel_", "trade_sel_", "_confirm_")))

    # ── Single source of truth: consume health_data computed in render_monitor_view ──
    health_data = st.session_state.get("system_health_data")
    cached = st.session_state.get("cached_header_data")

    if (not auto_refresh or wizard_active) and cached:
        _render_header_ui(cached)
        return

    try:
        if health_data and health_data.get("header_metrics"):
            hm = health_data["header_metrics"]
            data = {
                'total_equity':      hm.get('total_equity', 0.0),
                'futures_balance':   hm.get('futures_balance', 0.0),
                'global_pnl_usd':    hm.get('global_pnl_usd', 0.0),
                'total_invested_db': hm.get('total_invested_db', 0.0),
                'active_count':      hm.get('active_count', 0),
                'bots_in_trade':     hm.get('bots_in_trade', 0),
                'scanning_count':    hm.get('scanning_count', 0),
                'open_qty_notional': hm.get('open_qty_notional', 0.0),
                'assets_breakdown':  hm.get('assets_breakdown', []),
                'adoptions_today':   hm.get('adoptions_today', 0),
                'last_act_str':      hm.get('last_act_str', 'NO RECENT ACTIVITY'),
                # Status pill fields — sourced from health_data root, not header_metrics
                'system_status':     health_data.get('system_status', 'UNKNOWN'),
                'worst_gap_usd':     health_data.get('worst_gap_usd', 0.0),
            }
        else:
            # Fallback: compute locally if health_data not yet available
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
            active_count = cur.fetchone()[0]
            cur.execute("SELECT SUM(total_invested) FROM trades WHERE total_invested > 0")
            r = cur.fetchone(); total_invested_db = float(r[0] or 0.0)
            cur.execute(
                "SELECT COUNT(*) FROM trades t JOIN bots b ON b.id=t.bot_id "
                "WHERE b.is_active=1 AND t.total_invested > 0.01"
            )
            bots_in_trade = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COALESCE(SUM(t.open_qty * t.avg_entry_price), 0) "
                "FROM trades t JOIN bots b ON b.id=t.bot_id "
                "WHERE b.is_active=1 AND t.open_qty > 1e-8 AND t.avg_entry_price > 0"
            )
            open_qty_notional = float(cur.fetchone()[0] or 0.0)
            cur.execute("SELECT COUNT(*) FROM reconciliation_logs WHERE action LIKE '%ADOPTION%' AND timestamp > ?", (int(time.time()) - 86400,))
            adoptions_today = cur.fetchone()[0]
            cur.execute("SELECT action, symbol, price FROM trade_history ORDER BY id DESC LIMIT 1")
            last_h = cur.fetchone()
            last_act_str = f"{last_h[0]}: {last_h[1]} @ {last_h[2]:,.2f}" if last_h else "NO RECENT ACTIVITY"
            futures_balance = 0.0
            try:
                fut_data = fetch_balance_cached('future')
                if fut_data and 'total' in fut_data:
                    for asset, amount in fut_data['total'].items():
                        if amount and amount > 0 and asset in ('USDT', 'USDC', 'USD', 'BUSD'):
                            futures_balance += amount
            except Exception: pass
            data = {
                'total_equity': futures_balance, 'futures_balance': futures_balance,
                'global_pnl_usd': 0.0, 'total_invested_db': total_invested_db,
                'active_count': active_count, 'bots_in_trade': bots_in_trade,
                'scanning_count': max(0, active_count - bots_in_trade),
                'open_qty_notional': open_qty_notional, 'assets_breakdown': [],
                'adoptions_today': adoptions_today, 'last_act_str': last_act_str,
                # Fallback: status unknown until health_data is populated
                'system_status': 'UNKNOWN', 'worst_gap_usd': 0.0,
            }

        st.session_state["cached_header_data"] = data
        _render_header_ui(data)
    except Exception as e:
        st.error(f"Dashboard Load Error: {e}")


@st.fragment(run_every=5)
def _bot_positions_fragment():
    auto_refresh = st.session_state.get("auto_refresh_toggle", True)
    wizard_active = any(bool(st.session_state.get(k)) for k in st.session_state if k.startswith(("forensic_trades_", "adopt_force_sel_", "trade_sel_", "_confirm_")))

    # Retrieve cache if auto-refresh is off or wizard is active
    cached = st.session_state.get("cached_monitor_data")
    if (not auto_refresh or wizard_active) and cached:
        df_pos_f, df_physical_f, market_orders_f, df_h_f, ex_err = cached
        st.caption(f"  ⚡ Grid Sync (Cached): {time.strftime('%H:%M:%S')}")
    else:
        df_pos_f, df_physical_f, market_orders_f, df_h_f, ex_err = _fetch_fresh_monitor_data()
        st.caption(f"  ⚡ Grid Sync: {time.strftime('%H:%M:%S')}")
        st.session_state["cached_monitor_data"] = (df_pos_f, df_physical_f, market_orders_f, df_h_f, ex_err)
    
    if ex_err:
        st.warning(f"⚠️ {ex_err}")
    
    # --- Mismatch Alert Logic (v3.0.9) ---
    # Integrated directly into fragment for real-time parity tracking
    try:
        # Initialize metrics
        hedge_amounts = {}
        physical_qty_by_pair = {}
        pair_prices = {} 
        virtual_net_by_norm = {}
        mismatched_pairs = []

        # Pre-calculate hedge_amounts
        if not df_h_f.empty:
            for b_id in df_pos_f['id'].unique():
                h_sum = df_h_f[(df_h_f['bot_id'] == b_id) & (df_h_f['order_type'] == 'hedge')]['filled_amount'].sum()
                hx_sum = df_h_f[(df_h_f['bot_id'] == b_id) & (df_h_f['order_type'] == 'hedge_tp')]['filled_amount'].sum()
                hedge_amounts[b_id] = max(0.0, h_sum - hx_sum)

        hedged_bot_ids = set(df_h_f[df_h_f['filled_amount'] > 1e-8]['bot_id'].unique())
        
        # Pre-calculate physical order counts and lists for health checks
        physical_order_counts = {}
        physical_orders_for_bot = {}
        for o in market_orders_f:
            cid = str(o.get('clientOrderId') or '')
            if cid.startswith('CQB_'):
                try:
                    parts = cid.split('_')
                    if len(parts) >= 2:
                        bid_parsed = int(parts[1])
                        physical_order_counts[bid_parsed] = physical_order_counts.get(bid_parsed, 0) + 1
                        if bid_parsed not in physical_orders_for_bot:
                            physical_orders_for_bot[bid_parsed] = []
                        physical_orders_for_bot[bid_parsed].append(o)
                except: pass

        # Apply Display Status Mapping
        def derive_status(row):
            if not row['is_active']: return "⚪ STOPPED"
            b_status = str(row.get('status', '')).upper()
            if 'REQUIRE_MANUAL' in b_status: return "🚨 MANUAL GATE"
            if 'CARRY_PENDING' in b_status: return "⏳ CARRY/PENDING"
            
            c_phase = str(row.get('cycle_phase', 'IDLE')).upper()
            c_step = int(row.get('current_step', 0) if pd.notna(row.get('current_step')) else 0)
            invested = float(row.get('total_invested', 0) or 0)
            
            if c_phase == 'MARGIN_HELD':
                return f"🚫 MARGIN HELD | Step {c_step}"
            
            # Consistent threshold for 'In Trade'
            if c_phase == 'ACTIVE' or invested > 0.01:
                if invested > 0 and invested <= 5.0: return "🟡 DUST/PARTIAL"
                if row.get('bot_type') == 'hedge_child':
                    return f"🔴 HEDGE ACTIVE | Step {c_step}"
                return f"🔴 IN TRADE | Step {c_step}"
            
            if row.get('bot_type') == 'hedge_child':
                trigger_step = row.get('parent_hedge_trigger_step')
                if pd.notna(trigger_step) and trigger_step is not None:
                    return f"🟢 HEDGE STANDBY (Step {int(trigger_step)})"
                return "🟢 HEDGE STANDBY"
            
            return "🟢 SCANNING"

        df_pos_f['status'] = df_pos_f.apply(derive_status, axis=1)
        df_pos_f['Active Orders'] = df_pos_f['id'].apply(lambda x: physical_order_counts.get(int(x), 0))
        
        # Highlight missing orders per-row
        def highlight_health(row):
            if pd.isna(row.get('id')):
                return str(row.get('status', ''))
            bid, inv = int(row['id']), float(row['total_invested'] or 0)
            ord_count = physical_order_counts.get(bid, 0)
            status = str(row['status'])
            if ("IN TRADE" in status or "HEDGE ACTIVE" in status) and ord_count == 0 and "CARRY" not in str(row.get('cycle_phase','')):
                if row.get('bot_type') == 'hedge_child':
                    parent_id = row.get('parent_bot_id')
                    if parent_id:
                        parent_rows = df_pos_f[df_pos_f['id'] == parent_id]
                        if not parent_rows.empty:
                            parent_status = str(parent_rows.iloc[0]['status'])
                            if "IN TRADE" in parent_status:
                                return status
                # Grace period: suppress warning if engine just acted on this bot
                last_order_time = 0.0
                try:
                    conn_local = get_connection()
                    last_order = conn_local.execute(
                        "SELECT MAX(created_at) FROM bot_orders WHERE bot_id = ?",
                        (bid,)
                    ).fetchone()
                    if last_order and last_order[0] is not None:
                        last_order_time = float(last_order[0])
                except Exception:
                    pass
                last_order_age = time.time() - last_order_time
                if last_order_age < 60:
                    return status
                return f"⚠️ {status}"
            return status
        
        df_pos_f['status'] = df_pos_f.apply(highlight_health, axis=1)
        df_pos_f['sort_priority'] = df_pos_f['status'].apply(
            lambda x: 1 if ("IN TRADE" in x or "HEDGE ACTIVE" in x or "DUST" in x)
            else (2 if ("SCANNING" in x or "HEDGE STANDBY" in x) else 3)
        )
        df_pos_f.sort_values(by=['sort_priority', 'name'], ascending=[True, True], inplace=True)

        # ═══════════════════════════════════════════════════════════════════════
        # [v3.1.4] GLOBAL NETTING — SINGLE SOURCE OF TRUTH FIX
        # ═══════════════════════════════════════════════════════════════════════
        # BEFORE (broken): Built virtual_qty_by_pair from trades.open_qty +
        # inline hedge arithmetic. trades.open_qty is a cached snapshot that is
        # NOT updated synchronously when hedges fill, causing false-positive
        # mismatches (e.g. XRP: System=-743 vs Exchange=0 when hedge filled).
        #
        # AFTER (correct): Use get_pair_virtual_net(pair) — the authoritative
        # proof-only computation from actual bot_orders fills. It already handles
        # entries, exits, partial-cancel-fills, hedges, hedge_tp, wipe_wall, and
        # cycle_id boundaries. One function → one source of truth → no false alarms.
        # ═══════════════════════════════════════════════════════════════════════
        from engine.database import get_pair_virtual_net as _get_virtual_net

        # Build unique pair map: normalized_key → canonical DB pair string
        unique_db_pairs = {}  # p_key → canonical pair (e.g. 'XRP/USDC:USDC')
        for _, row in df_pos_f.iterrows():
            p_key = _norm_universal(row['pair'])
            if p_key not in unique_db_pairs:
                unique_db_pairs[p_key] = row['pair']
            avg = float(row.get('avg_entry_price') or 0)
            if avg > 0 and p_key not in pair_prices:
                pair_prices[p_key] = avg

        # Populate virtual_net_by_norm from authoritative source
        for p_key, canonical_pair in unique_db_pairs.items():
            try:
                virtual_net_by_norm[p_key] = _get_virtual_net(canonical_pair)
            except Exception as _vne:
                virtual_net_by_norm[p_key] = 0.0

        # Physical net: prefer LIVE exchange positions (one-way signed contracts).
        # Summing active_positions LONG/SHORT rows can double-count when SNAP-ALLOCATE
        # splits one net exchange position across multiple bot_id rows.
        live_physical_net_by_pair = {}
        try:
            for _pos in (fetch_positions_cached(global_config.MARKET_TYPE) or []):
                _amt = float(_pos.get('contracts', 0) or _pos.get('size', 0) or 0)
                if abs(_amt) < 1e-12:
                    continue
                _pkey = _norm_universal(_pos.get('symbol', ''))
                live_physical_net_by_pair[_pkey] = live_physical_net_by_pair.get(_pkey, 0.0) + _amt
                _epx = float(_pos.get('entryPrice', 0) or 0)
                if _epx > 0 and _pkey not in pair_prices:
                    pair_prices[_pkey] = _epx
        except Exception as _lp_err:
            st.caption(f"⚠️ Live position fetch failed, using DB snapshot: {_lp_err}")

        if not live_physical_net_by_pair and not df_physical_f.empty:
            for _, row in df_physical_f.iterrows():
                if pd.notna(row['size']) and pd.notna(row['entry_price']):
                    qty, price, side = abs(float(row['size'])), float(row['entry_price']), str(row['side']).upper().strip()
                    s_key, p_key = ('LONG' if side in ('BUY', 'LONG') else 'SHORT'), _norm_universal(row['pair'])
                    if p_key not in pair_prices:
                        pair_prices[p_key] = price
                    signed = qty if s_key == 'LONG' else -qty
                    live_physical_net_by_pair[p_key] = live_physical_net_by_pair.get(p_key, 0.0) + signed

        # 🔥 HEATMAP: Enrich pair_prices with LIVE ticker prices
        # Overwrite stale avg_entry_price with real mark prices for accurate distance %
        try:
            ex_live = get_exchange_instance(global_config.MARKET_TYPE)
            for p_key, canonical_pair in unique_db_pairs.items():
                try:
                    live_px = ex_live.get_last_price(canonical_pair)
                    if live_px and float(live_px) > 0:
                        pair_prices[p_key] = float(live_px)
                except Exception:
                    pass
        except Exception:
            pass

        # Union of all known symbols (virtual + physical)
        all_symbols = set(unique_db_pairs.keys())
        if not df_physical_f.empty:
            all_symbols |= set(_norm_universal(p) for p in df_physical_f['pair'])

        worst_pair_usd = 0.0
        mismatched_pair_count = 0

        for p in sorted(all_symbols):
            v_net_qty = virtual_net_by_norm.get(p, 0.0)
            ph_net_qty = live_physical_net_by_pair.get(p, 0.0)
            whitelists = get_manual_whitelists(p)
            for w in whitelists: ph_net_qty -= float(w['qty']) if w['side'] == 'LONG' else -float(w['qty'])
            
            ref_price = pair_prices.get(p, 1.0)
            net_qty_diff = abs(v_net_qty - ph_net_qty)
            net_usd_diff = net_qty_diff * ref_price
            if net_usd_diff > worst_pair_usd:
                worst_pair_usd = net_usd_diff
            # Pass/fail in contract qty space (same rule as engine parity gates).
            if net_qty_diff > pair_qty_tolerance():
                mismatched_pair_count += 1
                mismatched_pairs.append((f"{p} NET", v_net_qty * ref_price, ph_net_qty * ref_price, net_usd_diff, v_net_qty, ph_net_qty, ph_net_qty - v_net_qty, ref_price))

        # --- FRAGMENT UI RENDERING ---
        # ── FIXED-HEIGHT ALERT BANNER ──────────────────────────────────────────
        # Pre-collect all alert parts so we can decide healthy vs error BEFORE
        # rendering.  The banner container is ALWAYS rendered at a fixed height
        # (58 px) so the bot table below never shifts position when alerts appear
        # or disappear between refresh cycles.
        #
        # _banner_parts is populated here (mismatch) and extended after the
        # order-health loop below, then rendered as a single HTML block.
        # ──────────────────────────────────────────────────────────────────────
        _banner_parts = []
        if mismatched_pair_count > 0:
            for row_mp in mismatched_pairs:
                mp_pair, mp_virt_usd, mp_phys_usd, mp_diff_usd, mp_vqty, mp_pqty, mp_dqty, mp_price = row_mp
                clean_pair_name = mp_pair.replace(" NET", "")
                
                # Retrieve active bot names on this pair from df_pos_f
                pair_bots = df_pos_f[df_pos_f['pair'] == clean_pair_name]
                bot_names = [row_b['name'] for _, row_b in pair_bots.iterrows()]
                bot_info = f" ({', '.join(bot_names)})" if bot_names else ""
                
                _banner_parts.append(
                    f"🔴 Mismatch on {clean_pair_name}{bot_info}: "
                    f"sys={mp_vqty:+.4f} ex={mp_pqty:+.4f} diff={mp_dqty:+.4f} (Gap: ${mp_diff_usd:,.2f})"
                )
        # Critical bot states from health_data (GTR #1 + #4) — surfaced inside
        # the 15 s fragment so operators see them without waiting for header refresh.
        _hd = st.session_state.get("system_health_data") or {}
        for _bot_name in _hd.get("manual_proof_bots", []):
            is_netting_mismatch = False
            try:
                bot_row = df_pos_f[df_pos_f['name'] == _bot_name]
                if not bot_row.empty:
                    pair_raw = bot_row.iloc[0]['pair']
                    pair_norm = _norm_universal(pair_raw)
                    net_status = _hd.get("netting_status_per_pair", {}).get(pair_norm, {})
                    if net_status.get("drift_detected"):
                        is_netting_mismatch = True
            except:
                pass
            
            if is_netting_mismatch:
                _banner_parts.append(f"🔴 REQUIRE_MANUAL_PROOF: {_bot_name} — ⚠️ 軋平差額偏離（Netting Mismatch），請手動平倉或核對倉位")
            else:
                _banner_parts.append(f"🔴 REQUIRE_MANUAL_PROOF: {_bot_name} — human intervention needed")
        for _bot_name in _hd.get("stuck_cascade_bots", []):
            _banner_parts.append(f"⚠️ STUCK CASCADE >5m: {_bot_name}")
        # _order_alert will be appended after the order-health loop
        # (see 'Render fixed-height banner' section below)

        # Order Health Alerts
        order_health_msg = ""
        order_status_color = "green"

        bots_with_missing_orders = []
        bots_with_no_exit_orders = []
        bots_with_partial_orders = []
        bots_with_margin_held = []
        bots_with_stuck_dust = []
        for _, row in df_pos_f.iterrows():
            bid, bot_inv, c_step = int(row['id']), float(row['total_invested'] or 0), int(row.get('current_step', 0))
            actual_ph = physical_order_counts.get(bid, 0)
            
            # Skip bots that are legitimately idle or finishing
            if "EXITING" in str(row.get('status','')).upper() or ("SCANNING" in str(row.get('status','')).upper() and bot_inv <= 0.01):
                continue
                
            cycle_phase = str(row.get('cycle_phase', 'IDLE')).upper()

            if cycle_phase == 'STUCK_DUST_NO_EXIT':
                bots_with_stuck_dust.append(row['name'])

            # Check specifically for missing TP/exit orders for bots IN TRADE (INV-35)
            bot_type_str = str(row.get('bot_type', ''))
            is_hedge_bot = bot_type_str == 'hedge_child' or 'hedge' in bot_type_str.lower()
            critical_gap_type = None
            if bid not in hedged_bot_ids and not is_hedge_bot:
                has_tp = any(
                    o for o in physical_orders_for_bot.get(bid, [])
                    if o.get('order_type') == 'tp' or 'TP' in str(o.get('clientOrderId') or '')
                )
                if not has_tp and bot_inv > 0.01 and str(row.get('status','')).upper() == 'IN TRADE':
                    dur_days = 0.0
                    try:
                        from engine.database import get_connection as _gc_dur
                        with _gc_dur() as _conn_dur:
                            _t_row = _conn_dur.execute(
                                "SELECT updated_at FROM trades WHERE bot_id=?", (bid,)
                            ).fetchone()
                            if _t_row and _t_row[0]:
                                dur_days = round((time.time() - float(_t_row[0])) / 86400.0, 1)
                    except:
                        pass
                    bots_with_no_exit_orders.append(f"{row['name']} (in trade {dur_days} days, fully exposed)")
                    bots_with_missing_orders.append(row['name'])
                    critical_gap_type = 'NO_TP'

            if bid in hedged_bot_ids:
                # A bot in pure HEDGED phase legitimately has 0 physical orders:
                # its entry TP has been closed and it's holding a hedge SHORT
                # on the exchange, dormant, waiting for the hedge exit signal.
                # Only flag as MISSING if it's in HEDGE_EXIT_PENDING (actively
                # unwinding) but has no close/exit order placed.
                if cycle_phase == 'HEDGE_EXIT_PENDING' and actual_ph == 0:
                    bots_with_missing_orders.append(f"{row['name']} (HEDGE_EXIT no order)")
                # If in pure HEDGED: no alert — it's intentionally dormant
            elif cycle_phase == 'MARGIN_HELD':
                # Engine tried to place orders but Binance rejected with margin insufficiency.
                # This is not a missing orders bug — it's a real account-level constraint.
                # Show as orange warning, not red critical.
                bots_with_margin_held.append(f"{row['name']}")
            else:
                is_missing = False
                if actual_ph == 0 and bot_inv > 0.01 and cycle_phase != 'CARRY_PENDING' and critical_gap_type != 'NO_TP':
                    is_missing = True
                    if row.get('bot_type') == 'hedge_child':
                        parent_id = row.get('parent_bot_id')
                        if parent_id:
                            parent_rows = df_pos_f[df_pos_f['id'] == parent_id]
                            if not parent_rows.empty:
                                parent_status = str(parent_rows.iloc[0]['status'])
                                if "IN TRADE" in parent_status:
                                    is_missing = False
                if is_missing:
                    # Grace period: suppress warning if engine just acted on this bot
                    last_order_time = 0.0
                    try:
                        conn_local = get_connection()
                        last_order = conn_local.execute(
                            "SELECT MAX(created_at) FROM bot_orders WHERE bot_id = ?",
                            (bid,)
                        ).fetchone()
                        if last_order and last_order[0] is not None:
                            last_order_time = float(last_order[0])
                    except Exception:
                        pass
                    last_order_age = time.time() - last_order_time
                    if last_order_age < 60:
                        pass  # suppress warning
                    else:
                        bots_with_missing_orders.append(row['name'])
                elif actual_ph == 0 and cycle_phase == 'CARRY_PENDING':
                    pass # Engine is intentionally holding without orders
                elif c_step >= 1 and bot_inv > 0.01 and row.get('bot_type', 'standard') == 'standard':
                    try:
                        cfg_dict = json.loads(row.get('config') or '{}')
                        max_steps = int(cfg_dict.get('max_steps', 8))
                    except:
                        max_steps = 8
                    if c_step < max_steps:
                        # Query database to check if there is an active grid order for step = c_step + 1
                        has_grid = False
                        try:
                            conn_local = get_connection()
                            has_grid = conn_local.execute(
                                "SELECT COUNT(*) FROM bot_orders "
                                "WHERE bot_id = ? AND step = ? AND order_type = 'grid' "
                                "AND status IN ('open', 'new')",
                                (bid, c_step + 1)
                            ).fetchone()[0] > 0
                        except Exception:
                            pass

                        if not has_grid:
                            # 🚀 NETTING GATE GUARD: Suppress alert if grid is legitimately blocked by one-way netting opposite entry block
                            gow_ok = True
                            try:
                                from engine.oneway_netting import gate_oneway_opposite_entry
                                gow_ok, _ = gate_oneway_opposite_entry(bid, row['pair'], row['direction'])
                            except Exception:
                                pass
                            if gow_ok:
                                # Grace period: suppress warning if engine just acted on this bot
                                from engine.database import bot_has_recent_order_activity
                                if bot_has_recent_order_activity(bid, 60, conn_local):
                                    pass  # suppress warning
                                else:
                                    # Genuinely missing grid — emit warning
                                    bots_with_partial_orders.append(f"{row['name']} ({actual_ph}/2)")

        if bots_with_stuck_dust:
            for _dust_bot in bots_with_stuck_dust:
                _banner_parts.append(
                    f"🔴 【殘餘部位無法自動平倉】{_dust_bot}｜"
                    f"請至交易所手動平倉後，執行 safe_wipe_bot(action_label='MANUAL_CLOSE') 重置狀態。"
                )
        elif bots_with_no_exit_orders:
            _banner_parts.append(f"⚠️ NO EXIT ORDER: {', '.join(bots_with_no_exit_orders)}")
        elif bots_with_missing_orders:
            _banner_parts.append(f"⚠️ MISSING ORDERS: {', '.join(bots_with_missing_orders)}")
        elif bots_with_margin_held:
            _banner_parts.append(f"⚠️ MARGIN HELD: {', '.join(bots_with_margin_held)}")
        elif bots_with_partial_orders:
            _banner_parts.append(f"⚠️ MISSING GRIDS: {', '.join(bots_with_partial_orders)}")
        # If _banner_parts is empty → system is fully healthy → show nothing but
        # keep an identical-height spacer so the table below does not shift.

        # ── Render fixed-height banner ──────────────────────────────────────
        # DESIGN CONTRACT: both branches produce a 58-px-tall block so that
        # the bot table anchor point is stable across every refresh tick.
        _BANNER_H = "58px"
        if _banner_parts:
            _alert_html = (
                f'<div style="height:{_BANNER_H};background:rgba(255,75,75,0.12);'
                f'border:1px solid rgba(255,75,75,0.6);border-radius:0.5rem;'
                f'padding:0 1rem;display:flex;align-items:center;justify-content:space-between;'
                f'color:#FF4B4B;font-size:0.88rem;font-weight:500;'
                f'margin-bottom:0.75rem;overflow:hidden;white-space:nowrap;'
                f'text-overflow:ellipsis">'
                f'<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                f"🚨&nbsp;&nbsp;" + "&nbsp;&nbsp;|&nbsp;&nbsp;".join(_banner_parts) + "</div>"
                f'<span style="font-size:0.75rem;opacity:0.8;white-space:nowrap;margin-left:1rem">'
                f"Updated: {time.strftime('%H:%M:%S')}</span>"
                + "</div>"
            )
        else:
            # Active green status confirmation — identical height to prevent shifting
            _alert_html = (
                f'<div style="height:{_BANNER_H};background:rgba(75,255,75,0.06);'
                f'border:1px solid rgba(75,255,75,0.3);border-radius:0.5rem;'
                f'padding:0 1rem;display:flex;align-items:center;justify-content:space-between;'
                f'color:#2ECC71;font-size:0.88rem;font-weight:500;'
                f'margin-bottom:0.75rem;overflow:hidden;white-space:nowrap;'
                f'text-overflow:ellipsis">'
                f'<div>🟢&nbsp;&nbsp;All systems aligned and reconciled</div>'
                f'<span style="font-size:0.75rem;opacity:0.8;white-space:nowrap;margin-left:1rem">'
                f"Checked: {time.strftime('%H:%M:%S')}</span>"
                f'</div>'
            )
        st.markdown(_alert_html, unsafe_allow_html=True)

        # --- Global Netting Diagnostics (Restored to Fragment) ---
        try:
            with st.expander("🔍 Global Netting Diagnostics", expanded=False):
                st.write("**Reconciliation Mode:** Global Net (Hedge-Aware)")
                debug_data = []
                for p_dbg in sorted(all_symbols):
                    v_dbg = virtual_net_by_norm.get(p_dbg, 0.0)
                    ph_net_dbg = live_physical_net_by_pair.get(p_dbg, 0.0)
                    ph_l_dbg = ph_net_dbg if ph_net_dbg > 0 else 0.0
                    ph_s_dbg = abs(ph_net_dbg) if ph_net_dbg < 0 else 0.0
                    ph_net_dbg = ph_l_dbg - ph_s_dbg
                    debug_data.append({
                        "Symbol": p_dbg, "System Net": f"{v_dbg:+.4f}",
                        "Exchange Net": f"{ph_net_dbg:+.4f}", "Diff Qty": f"{abs(v_dbg - ph_net_dbg):.4f}"
                    })
                st.table(pd.DataFrame(debug_data))
        except: pass

        if mismatched_pairs:
            st.error("🚨 SYSTEM MISMATCH DETECTED")
            for row_mp in mismatched_pairs:
                mp_pair, mp_virt, mp_phys, mp_diff, mp_vqty, mp_pqty, mp_dqty, mp_price = row_mp
                st.warning(f"   ⚠️ **{mp_pair}**: System ${mp_virt:,.2f} vs Exchange ${mp_phys:,.2f} (Diff: ${mp_diff:,.2f}) | Qty: sys={mp_vqty:+.4f} ex={mp_pqty:+.4f} diff={mp_dqty:.4f}")
                
                # ── Confirmation-gate keys ──────────────────────────────────
                _ck_adopt  = f"_confirm_adopt_{mp_pair}"
                _ck_manual = f"_confirm_manual_{mp_pair}"
                _ck_close  = f"_confirm_close_{mp_pair}"
                _ck_orphan = f"_confirm_orphan_{mp_pair}"
                
                _p_clean = mp_pair.split(' ')[0]
                pair_bots = df_pos_f[df_pos_f['pair'].apply(_norm_universal) == _p_clean]
                can_close_orphan = True
                for _, bot_row in pair_bots.iterrows():
                    invested = float(bot_row.get('total_invested', 0) or 0)
                    derived_status = str(bot_row.get('status', '')).upper()
                    c_phase = str(bot_row.get('cycle_phase', 'IDLE')).upper()
                    is_scanning_or_idle = ("SCANNING" in derived_status) or ("IDLE" in derived_status) or (c_phase == "IDLE")
                    if invested > 0.01 or not is_scanning_or_idle:
                        can_close_orphan = False
                        break
                
                if can_close_orphan:
                    _act_c1, _act_c2, _act_c3, _act_c4 = st.columns(4)
                else:
                    _act_c1, _act_c2, _act_c3 = st.columns(3)
                    _act_c4 = None

                # ── 🕵️ Forensic Adopt (2-step) ───────────────────────────────
                with _act_c1:
                    if not st.session_state.get(_ck_adopt):
                        if st.button("🕵️ Forensic Adopt", key=f"f_{mp_pair}"):
                            st.session_state[_ck_adopt] = True
                            st.rerun()
                    else:
                        st.caption(f"⚠️ Adopt exchange qty into ledger for **{mp_pair}**?")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.button("✅ Confirm Adopt", key=f"fc_{mp_pair}", type="primary"):
                                st.session_state[_ck_adopt] = False
                                from engine.reconciler import StateReconciler
                                _p_clean = mp_pair.split(' ')[0]
                                clear_manual_whitelists_for_pair(_p_clean)
                                StateReconciler().perform_forensic_reconstruction(_p_clean)
                                st.rerun()
                        with cc2:
                            if st.button("❌ Cancel", key=f"fcancel_{mp_pair}"):
                                st.session_state[_ck_adopt] = False
                                st.rerun()

                # ── 📝 Manual Whitelist (2-step) ─────────────────────────────
                with _act_c2:
                    if not st.session_state.get(_ck_manual):
                        if st.button("📝 Manual", key=f"m_{mp_pair}"):
                            st.session_state[_ck_manual] = True
                            st.rerun()
                    else:
                        _side_str = 'LONG' if mp_dqty > 0 else 'SHORT'
                        st.caption(f"⚠️ Whitelist {_side_str} {abs(mp_dqty):.4f} for **{mp_pair.split(' ')[0]}**?")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.button("✅ Confirm Manual", key=f"mc_{mp_pair}", type="primary"):
                                st.session_state[_ck_manual] = False
                                add_manual_whitelist(mp_pair.split(' ')[0], _side_str, abs(mp_dqty))
                                st.rerun()
                        with cc2:
                            if st.button("❌ Cancel", key=f"mcancel_{mp_pair}"):
                                st.session_state[_ck_manual] = False
                                st.rerun()

                # ── 💥 Close / Reset (2-step, most dangerous) ───────────────
                with _act_c3:
                    if not st.session_state.get(_ck_close):
                        if st.button("💥 Close", key=f"c_{mp_pair}"):
                            st.session_state[_ck_close] = True
                            st.rerun()
                    else:
                        st.error(f"🚨 IRREVERSIBLE: Close exchange pos + reset ALL {mp_pair.split(' ')[0]} bots?")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.button("🔴 YES, CLOSE", key=f"cc_{mp_pair}"):
                                st.session_state[_ck_close] = False
                                _p_clean = mp_pair.split(' ')[0]
                                ex_m = get_exchange_instance('future')
                                clear_manual_whitelists_for_pair(_p_clean)
                                from engine.parity_gates import proof_flatten_pair
                                _ccxt_pair = _p_clean
                                if ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDC'):
                                    _ccxt_pair = f"{_ccxt_pair}:USDC"
                                elif ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDT'):
                                    _ccxt_pair = f"{_ccxt_pair}:USDT"
                                flat = proof_flatten_pair(
                                    ex_m, _ccxt_pair, human_approved=True,
                                )
                                if flat.get('success'):
                                    st.success(
                                        f"Proof flatten OK: cancelled {flat.get('cancelled_orders', 0)} orders, "
                                        f"reset {len(flat.get('bots_reset', []))} bots."
                                    )
                                else:
                                    st.error(flat.get('error', 'Proof flatten failed'))
                                    for err in flat.get('errors', []):
                                        st.warning(str(err))
                                st.rerun()
                        with cc2:
                            if st.button("❌ Cancel", key=f"ccancel_{mp_pair}"):
                                st.session_state[_ck_close] = False
                                st.rerun()

                # ── 💥 Close Orphan (2-step, reduceOnly order only) ───────────
                if _act_c4 is not None:
                    with _act_c4:
                        if not st.session_state.get(_ck_orphan):
                            if st.button("💥 Close Orphan", key=f"co_btn_{mp_pair}"):
                                st.session_state[_ck_orphan] = True
                                st.rerun()
                        else:
                            st.warning(f"🚨 CONFIRM: Close physical {abs(mp_pqty):.4f} {_p_clean} residual WITHOUT touching ledger?")
                            cco1, cco2 = st.columns(2)
                            with cco1:
                                if st.button("🔴 YES, FLAT", key=f"co_confirm_{mp_pair}"):
                                    st.session_state[_ck_orphan] = False
                                    ex_m = get_exchange_instance('future')
                                    clear_manual_whitelists_for_pair(_p_clean)
                                    _ccxt_pair = _p_clean
                                    if ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDC'):
                                        _ccxt_pair = f"{_ccxt_pair}:USDC"
                                    elif ':' not in _ccxt_pair and _ccxt_pair.endswith('/USDT'):
                                        _ccxt_pair = f"{_ccxt_pair}:USDT"
                                    
                                    close_side = 'sell' if mp_pqty > 0 else 'buy'
                                    close_qty = abs(mp_pqty)
                                    try:
                                        prec = ex_m.get_symbol_precision(_ccxt_pair)
                                        step = float(prec.get('amount_step', prec.get('step_size', 0)) or 0)
                                        if step > 0:
                                            close_qty = ex_m.round_to_step(close_qty, step)
                                    except Exception:
                                        pass
                                    
                                    if close_qty <= 0:
                                        st.error("Error: close_qty rounded to zero.")
                                    else:
                                        from engine.parity_gates import _repair_client_order_id
                                        client_id = _repair_client_order_id('CQB_ORPH', _ccxt_pair)
                                        try:
                                            close_order = ex_m.create_order(
                                                symbol=_ccxt_pair,
                                                type='market',
                                                side=close_side,
                                                amount=close_qty,
                                                price=None,
                                                params={
                                                    'reduceOnly': True,
                                                    'clientOrderId': client_id,
                                                    'human_approved': True,
                                                },
                                            )
                                            st.success(f"Orphan closed: {close_qty} {_ccxt_pair} market reduceOnly placed.")
                                        except Exception as _co_err:
                                            st.error(f"Market close failed: {_co_err}")
                                    st.rerun()
                            with cco2:
                                if st.button("❌ Cancel", key=f"co_cancel_{mp_pair}"):
                                    st.session_state[_ck_orphan] = False
                                    st.rerun()

        # --- Position Details & Grids ---
        st.subheader("🤖 Active Bot Positions")
        
        # Extract Trigger Info, Active Orders, and EE/Profit Metrics
        indicator_cache_f = {} # Per-fragment local cache
        def extract_info(row):
            res = {
                'Trigger': 'N/A', 'Orders': '0', 'TP_Price': 0.0,
                'Grid_Price': 0.0, 'Grid_Amount': 0.0,
                'Expected_Profit': 0.0, 'Expected_Profit_Str': '-', 'EE_Status': '-',
                'TP_Price_Str': '-', 'Grid_Price_Str': '-',
                'Action_Age': '-', 'Trade_Age': '-', 'Ages': '-',
            }
            def _clean(val):
                if pd.isna(val) or val is None: return 0.0
                try: return float(val)
                except: return 0.0
            try:
                cfg_raw = row.get('config')
                cfg = json.loads(cfg_raw if cfg_raw else '{}')
                # Fetch current market price
                pair_key = _norm_universal(row.get('pair', ''))
                current_price = _clean(pair_prices.get(pair_key, 0.0))
                
                inv = _clean(row.get('total_invested'))
                is_in_trade = inv > 0.01 or str(row.get('cycle_phase', '')).upper() == 'ACTIVE'

                if row.get('bot_type') == 'hedge_child' and not is_in_trade:
                    parent_name = row.get('parent_name')
                    if not parent_name or pd.isna(parent_name):
                        parent_name = 'parent'
                    trigger_step = row.get('parent_hedge_trigger_step')
                    if pd.notna(trigger_step) and trigger_step is not None:
                        res['Trigger'] = f"Awaiting parent '{parent_name}' step {int(trigger_step)}"
                    else:
                        res['Trigger'] = f"Awaiting parent '{parent_name}' signal"
                    return res
                
                # Helper for Indicators (Cached per fragment run)
                def get_indicator_val(p, tf, itype, period=14):
                    cache_key = (p, tf, itype, period)
                    if cache_key in indicator_cache_f: return indicator_cache_f[cache_key]
                    try:
                        ohlcv = fetch_ohlcv_cached(global_config.MARKET_TYPE, p, tf)
                        if not ohlcv: return None
                        df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                        from engine import indicators
                        if itype == 'RSI': val = float(indicators.rsi(df['c'], period).iloc[-1])
                        elif itype == 'CCI': val = float(indicators.cci(df['h'], df['l'], df['c'], period).iloc[-1])
                        else: val = None
                        indicator_cache_f[cache_key] = val
                        return val
                    except: return None

                # 1. Trigger Description (Entry Conditions)
                triggers = []
                m_p = int(cfg.get('mode_price', 0) or 0)
                t_p = float(cfg.get('price_threshold', 0) or 0)
                
                dist_pct = 0.0
                if current_price > 0 and t_p > 0:
                    dist_pct = abs(current_price - t_p) / t_p * 100
                    
                # Proximity Labels for Price Entry
                is_met = False
                if m_p == 1 and current_price >= t_p:
                    is_met = True
                elif m_p == 2 and current_price <= t_p:
                    is_met = True

                if is_met:
                    entry_label = "🟢 [MET]"
                else:
                    if dist_pct < 0.5: entry_label = "🟢 [IN RANGE]"
                    elif dist_pct < 2.0: entry_label = "🟡 [SOON]"
                    else: entry_label = "⚪ [FAR]"

                if m_p == 1: 
                    dist_str = f" ({((current_price - t_p)/t_p*100):.1f}%)" if current_price > 0 and t_p > 0 else ""
                    triggers.append(f"{entry_label} Price > ${t_p:,.2f}{dist_str}")
                elif m_p == 2: 
                    dist_str = f" ({((current_price - t_p)/t_p*100):.1f}%)" if current_price > 0 and t_p > 0 else ""
                    triggers.append(f"{entry_label} Price < ${t_p:,.2f}{dist_str}")
                    
                if cfg.get('mode_rsi'): 
                    target_rsi = float(cfg.get('rsi_level', 0))
                    tf_rsi = cfg.get('rsi_tf', '15m')
                    curr_rsi = get_indicator_val(row.get('pair'), tf_rsi, 'RSI')
                    if curr_rsi is not None:
                        dist_rsi = abs(curr_rsi - target_rsi)
                        label = "🟢 [IN RANGE]" if dist_rsi < 2 else ("🟡 [SOON]" if dist_rsi < 8 else "⚪ [FAR]")
                        triggers.append(f"{label} RSI({target_rsi}): {curr_rsi:.1f}")
                    else:
                        triggers.append(f"RSI({target_rsi})")

                if cfg.get('mode_cci'): 
                    target_cci = float(cfg.get('cci_level', 0))
                    tf_cci = cfg.get('cci_tf', '5m')
                    curr_cci = get_indicator_val(row.get('pair'), tf_cci, 'CCI')
                    if curr_cci is not None:
                        dist_cci = abs(curr_cci - target_cci)
                        label = "🟢 [IN RANGE]" if dist_cci < 15 else ("🟡 [SOON]" if dist_cci < 40 else "⚪ [FAR]")
                        triggers.append(f"{label} CCI({target_cci}): {curr_cci:.1f}")
                    else:
                        triggers.append(f"CCI({target_cci})")

                if cfg.get('mode_stoch'): 
                    tf_stoch = cfg.get('stoch_tf', '15m')
                    # Note: stochastic returns (%K, %D). We'll use %K.
                    try:
                        ohlcv = fetch_ohlcv_cached(global_config.MARKET_TYPE, row.get('pair'), tf_stoch)
                        if ohlcv:
                            df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                            from engine import indicators
                            k, d = indicators.stochastic(df['h'], df['l'], df['c'])
                            curr_k = float(k.iloc[-1])
                            label = "🟢 [IN RANGE]" if (curr_k < 20 or curr_k > 80) else "⚪ [FAR]"
                            triggers.append(f"{label} Stoch: {curr_k:.1f}")
                    except:
                        triggers.append(f"Stoch({tf_stoch})")

                if cfg.get('mode_boll'): 
                    tf_boll = cfg.get('boll_tf', '15m')
                    try:
                        ohlcv = fetch_ohlcv_cached(global_config.MARKET_TYPE, row.get('pair'), tf_boll)
                        if ohlcv:
                            df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
                            from engine import indicators
                            u, m, l = indicators.bollinger_bands(df['c'])
                            curr_p = float(df['c'].iloc[-1])
                            curr_u = float(u.iloc[-1])
                            curr_l = float(l.iloc[-1])
                            dist_u = abs(curr_p - curr_u) / curr_u * 100
                            dist_l = abs(curr_p - curr_l) / curr_l * 100
                            label = "🟢 [IN RANGE]" if (dist_u < 0.2 or dist_l < 0.2) else ("🟡 [SOON]" if (dist_u < 1.0 or dist_l < 1.0) else "⚪ [FAR]")
                            triggers.append(f"{label} Bollinger")
                    except:
                        triggers.append(f"Bollinger")
                
                desc_trigger = " + ".join(triggers) if triggers else "Trend/Dynamic"
                
                inv = _clean(row.get('total_invested'))
                is_in_trade = inv > 0.01 or str(row.get('cycle_phase', '')).upper() == 'ACTIVE'
                
                # 🎯 HEATMAP REFACTOR: If In Trade, show TP/Grid distance as primary status
                if is_in_trade:
                    # We'll calculate this after fetching orders below, so for now just placeholder
                    res['Trigger'] = desc_trigger
                else:
                    res['Trigger'] = desc_trigger
                
                # 2. Order Tracking
                bot_id = int(row['id'])
                my_orders = [o for o in market_orders_f if str(o.get('clientOrderId') or '').startswith(f"CQB_{bot_id}_")]
                if my_orders:
                    detailed = []
                    for o in my_orders:
                        cid = str(o.get('clientOrderId', ''))
                        price_val = _clean(o.get('price'))
                        if 'TP' in cid:
                            detailed.append('TP')
                            res['TP_Price'] = price_val
                        elif 'GRID' in cid:
                            detailed.append('GRID')
                            # If multiple grids, show the one closest to current price
                            if res['Grid_Price'] == 0:
                                res['Grid_Price'] = price_val
                                res['Grid_Amount'] = _clean(o.get('amount'))
                        elif 'ENTRY' in cid: detailed.append('ENTRY')
                        elif 'HEDGE' in cid: detailed.append('HEDGE')
                    res['Orders'] = f"{len(my_orders)} ({', '.join(detailed[:5])}{'...' if len(detailed)>5 else ''})"
                    
                # Enrich TP and Grid with distance
                if res['TP_Price'] > 0 and current_price > 0:
                    dist = (current_price - res['TP_Price']) / res['TP_Price'] * 100
                    res['TP_Price_Str'] = f"${res['TP_Price']:,.2f} ({dist:+.1f}%)"
                else:
                    res['TP_Price_Str'] = f"${res['TP_Price']:,.2f}" if res['TP_Price'] > 0 else "-"
                    
                if res['Grid_Price'] > 0 and current_price > 0:
                    dist = (current_price - res['Grid_Price']) / res['Grid_Price'] * 100
                    res['Grid_Price_Str'] = f"{res['Grid_Amount']:.4f} @ ${res['Grid_Price']:,.2f} ({dist:+.1f}%)"
                else:
                    res['Grid_Price_Str'] = f"{res['Grid_Amount']:.4f} @ ${res['Grid_Price']:,.2f}" if res['Grid_Price'] > 0 else "-"
                
                # 🎯 HEATMAP ENRICHMENT (Final Step):
                if is_in_trade:
                    parts = []
                    direction_upper = str(row.get('direction', 'LONG')).upper()

                    # — TP proximity —
                    if res['TP_Price'] > 0 and current_price > 0:
                        tp_dist = abs(current_price - res['TP_Price']) / res['TP_Price'] * 100
                        # For SHORT bots TP is below price; for LONG TP is above.
                        tp_signed = (res['TP_Price'] - current_price) / res['TP_Price'] * 100
                        if direction_upper == 'SHORT':
                            tp_signed = (current_price - res['TP_Price']) / res['TP_Price'] * 100
                        tp_label = "🟢 TP" if tp_dist < 0.2 else ("🟡 TP" if tp_dist < 1.0 else "⚪ TP")
                        parts.append(f"{tp_label} {tp_dist:.1f}% ({tp_signed:+.1f}%)")

                    # — Grid proximity (next grid order in the adverse direction) —
                    if current_price > 0:
                        # Scan all open orders for this bot to find the best grid candidate
                        bot_id_local = int(row['id'])
                        grid_candidates = [
                            o for o in market_orders_f
                            if str(o.get('clientOrderId', '')).startswith(f"CQB_{bot_id_local}_")
                            and 'GRID' in str(o.get('clientOrderId', ''))
                        ]
                        if grid_candidates:
                            # Pick the grid closest to current price
                            best_grid = min(
                                grid_candidates,
                                key=lambda o: abs(float(o.get('price', 0) or 0) - current_price)
                            )
                            grid_px = float(best_grid.get('price', 0) or 0)
                            grid_qty = float(best_grid.get('amount', 0) or 0)
                            if grid_px > 0:
                                grid_dist = abs(current_price - grid_px) / grid_px * 100
                                # For SHORT: grid is above price (adds to short)
                                # For LONG: grid is below price (adds to long)
                                grid_signed = (grid_px - current_price) / grid_px * 100
                                if direction_upper == 'SHORT':
                                    grid_signed = (current_price - grid_px) / grid_px * 100
                                grid_label = "🔴 GRID" if grid_dist < 0.3 else ("🟡 GRID" if grid_dist < 1.0 else "⚪ GRID")
                                parts.append(f"{grid_label} {grid_dist:.1f}% ({grid_signed:+.1f}%)")

                    if parts:
                        res['Trigger'] = " | ".join(parts)
                    else:
                        if row.get('bot_type') == 'hedge_child':
                            parent_id = row.get('parent_bot_id')
                            parent_name = row.get('parent_name') or "parent"
                            if parent_id:
                                parent_rows = df_pos_f[df_pos_f['id'] == parent_id]
                                if not parent_rows.empty:
                                    parent_status = str(parent_rows.iloc[0]['status'])
                                    if "IN TRADE" in parent_status:
                                        res['Trigger'] = f"Awaiting parent '{parent_name}' exit"
                                    else:
                                        res['Trigger'] = "⚠️ NO ORDERS"
                                else:
                                    res['Trigger'] = "⚠️ NO ORDERS"
                            else:
                                res['Trigger'] = "⚠️ NO ORDERS"
                        else:
                            res['Trigger'] = "⚠️ NO ORDERS"
                
                # 3. Expected Profit
                o_qty = _clean(row.get('open_qty'))
                tp_p = _clean(res['TP_Price'] or row.get('target_tp_price'))
                avg_p = _clean(row.get('avg_entry_price'))
                status = str(row.get('status', '')).upper()
                if 'SCANNING' not in status and o_qty > 1e-8 and tp_p > 1e-8 and avg_p > 1e-8:
                    side = str(row.get('direction', 'LONG')).upper()
                    if side == 'SHORT': res['Expected_Profit'] = (avg_p - tp_p) * o_qty
                    else: res['Expected_Profit'] = (tp_p - avg_p) * o_qty
                    
                    profit = res['Expected_Profit']
                    is_be = False
                    if cfg.get('UseEarlyExit', False):
                        if abs(tp_p - avg_p) <= 0.01 or (avg_p > 0 and (abs(tp_p - avg_p) / avg_p) < 0.0005):
                            is_be = True
                    
                    profit_val = 0.0 if abs(profit) < 0.005 else profit
                    if is_be:
                        res['Expected_Profit_Str'] = f"${profit_val:,.2f} ⚖️ BE"
                    else:
                        res['Expected_Profit_Str'] = f"${profit_val:,.2f}"
                else:
                    res['Expected_Profit_Str'] = "-"
                
                # 4. Early Exit (EE) Status — compact format
                if row.get('bot_type') == 'hedge_child':
                    res['EE_Status'] = "⏳ Parent TP pending"
                else:
                    b_start = _clean(row.get('basket_start_time'))
                    if is_in_trade and cfg.get('UseEarlyExit') and b_start > 0:
                        ee_start_h = _clean(cfg.get('EEStartHours'))
                        elapsed_h = (time.time() - b_start) / 3600
                        if elapsed_h > ee_start_h:
                            decay_mins = _clean(cfg.get('DecayIntervalMins', 15))
                            decay_pct = _clean(cfg.get('DecayPercentPerInterval', 10))
                            intervals = (elapsed_h - ee_start_h) * 60 / decay_mins
                            total_decay = min(100.0, intervals * decay_pct)
                            if total_decay >= 100.0:
                                res['EE_Status'] = "🔥100%"
                            else:
                                intervals_to_full = (100.0 - total_decay) / decay_pct
                                mins_to_full = intervals_to_full * decay_mins
                                h_to_full = mins_to_full / 60
                                ttf = f"{mins_to_full:.0f}m" if h_to_full < 1.0 else f"{h_to_full:.1f}h"
                                res['EE_Status'] = f"🔥{total_decay:.0f}%▸{ttf}"
                        else:
                            wait_h = ee_start_h - elapsed_h
                            wait_str = f"{wait_h*60:.0f}m" if wait_h < 1.0 else f"{wait_h:.1f}h"
                            res['EE_Status'] = f"⏳{wait_str}"

                # 5+6. Ages — merge pos age / cycle age into one field
                b_start = _clean(row.get('basket_start_time'))
                c_start = _clean(row.get('cycle_start_time'))
                def _fmt_age(secs_since_epoch):
                    if secs_since_epoch <= 0: return "-"
                    h = (time.time() - secs_since_epoch) / 3600
                    if h < 1.0: return f"{h*60:.0f}m"
                    if h < 24.0: return f"{h:.1f}h"
                    return f"{h/24:.1f}d"
                pos_age  = _fmt_age(b_start) if is_in_trade else "-"
                cyc_age  = _fmt_age(c_start) if is_in_trade else "-"
                # combined: "21m / 3.2h"  (pos / cycle)
                res['Ages'] = f"{pos_age}/{cyc_age}"

            except Exception as e:
                print(f"Error extracting info for bot {row.get('id')}: {e}")
            return res

        if not df_pos_f.empty:
            info_df = df_pos_f.apply(extract_info, axis=1, result_type='expand')
            df_pos_f['TP | Grid'] = info_df['Trigger']
            df_pos_f['Active Orders'] = info_df['Orders']
            df_pos_f['Active TP'] = info_df['TP_Price_Str']
            df_pos_f['Next Grid'] = info_df['Grid_Price_Str']

            df_pos_f['Exp $'] = info_df['Expected_Profit_Str']
            df_pos_f['EE'] = info_df['EE_Status']
            df_pos_f['Ages (pos/cyc)'] = info_df['Ages']
            df_pos_f['Total Invested'] = df_pos_f['total_invested'].apply(
                lambda x: f"${x:,.2f}" if x > 0.01 else "-"
            )
            df_pos_f['Open Qty'] = df_pos_f['open_qty'].apply(
                lambda x: f"{float(x):.4f}" if pd.notna(x) and float(x) > 1e-8 else "-"
            )
            df_pos_f['Avg Entry'] = df_pos_f['avg_entry_price'].apply(
                lambda x: f"${float(x):,.4f}" if pd.notna(x) and float(x) > 0 else "-"
            )

            # Unrealized PnL: (current_price - avg_entry) * open_qty, signed by direction
            def calc_unrealised(row):
                try:
                    inv = float(row.get('total_invested') or 0)
                    if inv <= 0.01:
                        return "-"
                    aq = float(row.get('open_qty') or 0)
                    ap = float(row.get('avg_entry_price') or 0)
                    pk = _norm_universal(row.get('pair', ''))
                    cp = float(pair_prices.get(pk, 0))
                    if aq < 1e-8 or ap < 1e-8 or cp < 1e-8:
                        return "-"
                    direction_u = str(row.get('direction', 'LONG')).upper()
                    if direction_u == 'SHORT':
                        pnl = (ap - cp) * aq
                    else:
                        pnl = (cp - ap) * aq
                    arrow = "▲" if pnl >= 0 else "▼"
                    return f"{arrow}${pnl:,.2f}"
                except:
                    return "-"

            df_pos_f['PnL'] = df_pos_f.apply(calc_unrealised, axis=1)

            # Entry trigger (only meaningful for scanning bots)
            df_pos_f['Entry Trigger'] = info_df['Trigger'].where(
                ~df_pos_f['status'].str.contains('IN TRADE|DUST|HEDGE ACTIVE', na=False),
                other=""
            )
        else:
            for col in ['TP | Grid', 'Active Orders', 'Active TP', 'Next Grid', 'Exp $', 'EE', 'Ages (pos/cyc)', 'Total Invested', 'Open Qty', 'Avg Entry', 'PnL', 'Entry Trigger']:
                df_pos_f[col] = pd.Series(dtype=object)

        st.dataframe(
            df_pos_f[[
                'name', 'pair', 'status',
                'Total Invested', 'Open Qty', 'Avg Entry', 'PnL',
                'Exp $', 'EE', 'Ages (pos/cyc)',
                'TP | Grid', 'Active TP', 'Next Grid',
            ]],
            column_config={
                'name':            st.column_config.TextColumn('Bot',       width='small'),
                'pair':            st.column_config.TextColumn('Pair',      width='small'),
                'status':          st.column_config.TextColumn('Status',    width='medium'),
                'Total Invested':  st.column_config.TextColumn('Invested',  width='small'),
                'Open Qty':        st.column_config.TextColumn('Qty',       width='small'),
                'Avg Entry':       st.column_config.TextColumn('Entry',     width='small'),
                'PnL':             st.column_config.TextColumn('PnL',       width='small'),
                'Exp $':           st.column_config.TextColumn('Exp $',     width='small'),
                'EE':              st.column_config.TextColumn('EE',        width='small'),
                'Ages (pos/cyc)':  st.column_config.TextColumn('Age p/c',   width='small'),
                'TP | Grid':       st.column_config.TextColumn('TP | Grid', width='medium'),
                'Active TP':       st.column_config.TextColumn('TP @',      width='small'),
                'Next Grid':       st.column_config.TextColumn('Grid @',    width='small'),
            },
            width='stretch',
            hide_index=True
        )

        # Manual Link Recovery Section
        active_bot_ids = [str(b_id) for b_id in df_pos_f['id'].values]
        stray_orders = [o for o in market_orders_f if str(o.get('clientOrderId','')).startswith('CQB_') and o.get('clientOrderId','').split('_')[1] not in active_bot_ids]
        
        # --- Orphan Position Detection (Restored to Fragment) ---
        try:
            _orphan_rows = sqlite3.connect(global_config.PATHS['DB_FILE']).execute("SELECT pair, side, size, entry_price FROM active_positions WHERE bot_id=0").fetchall()
            if _orphan_rows:
                st.error("🚨 Unowned Physical Positions (Orphans) Found!")
                for i, _or in enumerate(_orphan_rows):
                    _o_pair, _o_side, _o_size, _o_entry = _or
                    if st.button(f"💥 Flatten {_o_pair} {_o_side} ({_o_size})", key=f"flat_{_o_pair}_{i}"):
                        get_exchange_instance('future').create_order(_o_pair, 'market', 'sell' if _o_side.upper() == 'LONG' else 'buy', _o_size, params={'reduceOnly': True})
                        st.rerun()
        except: pass

        if stray_orders:
            with st.expander(f"🧙‍♂️ Manual Link Recovery Tool ({len(stray_orders)} Strays)", expanded=False):
                st.info("Found orders on exchange belonging to bots that are no longer active.")
                for i, o in enumerate(stray_orders):
                    st.write(f"• {o.get('symbol')} {o.get('side')} {o.get('amount')} @ {o.get('price')} (ID: {o.get('clientOrderId')})")
                    if st.button(f"Cancel {o.get('id')}", key=f"cancel_{o.get('id')}_{i}"):
                        get_exchange_instance('future').cancel_order(o.get('id'), o.get('symbol')); st.rerun()

    except Exception as e_frag:
        st.error(f"Fragment Error: {e_frag}")
        import traceback
        st.code(traceback.format_exc())

@st.fragment(run_every=15)
def _exchange_sync_diagnostics_fragment():
    import json
    import os
    import time
    from config.settings import config
    
    cache_file = os.path.join(config.ROOT_DIR, 'data', 'exchange_sync_diagnostics.json')
    if not os.path.exists(cache_file):
        st.info("No exchange sync diagnostics data available yet. Waiting for startup or reconciler cycle check.")
        return
        
    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)
    except Exception as e:
        st.error(f"Failed to read sync diagnostics: {e}")
        return
        
    if not data:
        st.info("No exchange sync data in cache.")
        return
        
    # Count drifting pairs
    drifting_pairs = [pair for pair, sync_data in data.items() if sync_data.get('drift_detected', False)]
    num_drifting = len(drifting_pairs)
    
    label = "🔍 Exchange Sync Diagnostics" if num_drifting == 0 else f"⚠️ Exchange Sync Diagnostics ({num_drifting} pairs drifting)"
    
    with st.expander(label, expanded=num_drifting > 0):
        if num_drifting == 0:
            st.markdown("✅ All pairs in sync")
        else:
            for pair in drifting_pairs:
                sync_data = data[pair]
                ts = sync_data.get('timestamp', 0)
                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
                drift_detected = sync_data.get('drift_detected', False)
                exchange_net = sync_data.get('exchange_net', 0.0)
                db_sum_qty = sync_data.get('db_sum_qty', 0.0)
                diff = sync_data.get('diff', 0.0)
                tolerance = sync_data.get('tolerance', 0.0)
                
                status_color = "red" if drift_detected else "green"
                status_text = "DRIFT DETECTED" if drift_detected else "IN SYNC"
                
                st.markdown(
                    f"### {pair} : :{status_color}[{status_text}]"
                )
                st.markdown(
                    f"**Last Checked:** {time_str} | **Tolerance:** {tolerance:.6f}\n\n"
                    f"**Exchange Net:** `{exchange_net:.8f}` | **DB sum(open_qty):** `{db_sum_qty:.8f}` | **Diff:** `{diff:.8f}`"
                )
                
                bots = sync_data.get('bots', [])
                if bots:
                    with st.expander(f"Contributing Bots for {pair} ({len(bots)} active)", expanded=drift_detected):
                        import pandas as pd
                        df = pd.DataFrame(bots)
                        df = df.rename(columns={
                            'bot_id': 'Bot ID',
                            'name': 'Bot Name',
                            'direction': 'Direction',
                            'open_qty': 'Open Qty',
                            'signed_qty': 'Signed Qty (Contribution)'
                        })
                        st.dataframe(df, width='stretch', hide_index=True)
                st.markdown("---")


@st.fragment(run_every=10)
def _notifications_fragment():
    try:
        # Initialize session state for notifications if not present
        if 'shown_notifications' not in st.session_state:
            st.session_state.shown_notifications = set()

        notes = get_unread_notifications(limit=5)
        if notes:
            n_ids_to_mark = []
            for n in notes:
                # n: id, timestamp, type, message, bot_id
                nid, _, ntype, msg, bid = n
                
                # Deduplicate: Only show if not already shown in this session (or handled by DB)
                if nid not in st.session_state.shown_notifications:
                    icon = "ℹ️"
                    if ntype == 'success': icon = "✅"
                    elif ntype == 'error': icon = "❌"
                    elif ntype == 'warning': icon = "⚠️"
                    
                    st.toast(msg, icon=icon)
                    st.session_state.shown_notifications.add(nid)
                    n_ids_to_mark.append(nid)
            
            if n_ids_to_mark:
                mark_notifications_read(n_ids_to_mark)
    except Exception:
        pass # Fail silently to keep UI responsive


def render_unowned_positions_banner():
    """
    Renders a warning banner and manual adoption flow for unowned exchange positions.

    Two sources are shown:
    1. Real-time orphan_positions from health_data (exchange != 0 AND no bot open_qty).
    2. DB-backed unowned_position_alerts table for the manual adoption workflow.

    Stale DB alerts created before the current ENGINE_STARTED_AT are suppressed so
    orphan data from previous sessions does not persist across engine restarts.
    """
    # ── (1) Real-time orphan positions from health_data ────────────────────────
    health_data = st.session_state.get("system_health_data") or {}
    orphans = health_data.get("orphan_positions", [])
    if orphans and not health_data.get("startup_suppression"):
        st.error(
            f"⚠️ **{len(orphans)} orphan exchange position(s) detected** — "
            "exchange holds a net position with no matching bot open_qty."
        )
        for o in orphans:
            st.write(
                f"• **{o['pair']}**: exchange_net `{o['exchange_net']:+.4f}` "
                f"≈ **${o['notional_usd']:,.2f}** — no bot open_qty accounts for this."
            )

    # ── (2) DB-backed adoption alerts (suppressed if created before engine start) ──
    engine_started_at = health_data.get("engine_started_at", 0.0)

    conn = get_connection()
    try:
        pending_alerts = conn.execute("""
            SELECT a.id, a.bot_id, a.pair, a.normalized_pair, a.exchange_qty, a.db_qty, a.notes,
                   COALESCE(a.created_at, 0) as created_at
            FROM unowned_position_alerts a
            WHERE a.status = 'pending_review'
        """).fetchall()
    except Exception:
        # Table might not exist or be locked
        return

    # Filter out stale alerts from previous sessions
    if engine_started_at > 0:
        pending_alerts = [
            a for a in pending_alerts
            if float(a[7] or 0) >= engine_started_at
        ]

    if not pending_alerts:
        return

    st.error(f"⚠️ **{len(pending_alerts)} unowned exchange positions detected — manual adoption required**")

    from engine.oneway_netting import get_typical_position_size

    for alert in pending_alerts:
        alert_id, alert_bot_id, pair, norm_pair, ex_qty, db_qty, notes, _created_at = alert
        shortfall = ex_qty - db_qty

        
        title = f"Orphan Position: {pair} | Drift: {shortfall:+.4f}"
        with st.expander(title, expanded=True):
            st.markdown(f"**Description**: {notes}")
            st.write(f"• **Exchange Net Position**: `{ex_qty:+.4f}`")
            st.write(f"• **Database Net Position**: `{db_qty:+.4f}`")
            st.write(f"• **Discovered Shortfall**: `{shortfall:+.4f}`")

            # Fetch all active bots on this pair for dropdown selection
            active_bots = conn.execute("""
                SELECT b.id, b.name, b.direction 
                FROM bots b
                WHERE b.is_active = 1 AND b.normalized_pair = ?
            """, (norm_pair,)).fetchall()
            
            if not active_bots:
                st.error("No active bots found on this pair to adopt the position.")
                continue

            bot_options = {f"{r[1]} ({r[0]}) | {r[2]}": r[0] for r in active_bots}
            
            # Determine default index
            default_index = 0
            if alert_bot_id is not None:
                for idx, (label, bid) in enumerate(bot_options.items()):
                    if bid == alert_bot_id:
                        default_index = idx
                        break

            # Always display dropdown for human override capability
            selected_label = st.selectbox(
                "Select Bot for Adoption", 
                options=list(bot_options.keys()), 
                index=default_index,
                key=f"select_bot_{alert_id}"
            )
            target_bot_id = bot_options[selected_label]
            
            if alert_bot_id is not None:
                st.info(f"Auto-matched Suggestion: **{selected_label}**")
            else:
                st.warning("No candidate bot automatically matched this position size/direction. Please select a bot manually.")

            # Fetch the selected/target bot details (cycle_id)
            bot_row = conn.execute("""
                SELECT b.name, t.cycle_id 
                FROM bots b JOIN trades t ON t.bot_id = b.id 
                WHERE b.id = ?
            """, (target_bot_id,)).fetchone()
            
            if not bot_row:
                st.error("Could not fetch target bot trade cycle.")
                continue
                
            bot_name, cycle_id = bot_row

            # Fetch average entry price from exchange for the pair
            avg_price = 0.0
            try:
                ex = get_exchange_instance('future')
                pos = ex.fetch_positions()
                for p in pos:
                    if p.get('symbol') == pair:
                        avg_price = float(p.get('entryPrice') or 0.0)
                        break
            except Exception:
                pass

            if avg_price <= 0.0:
                st.warning("Could not fetch a valid entry price from exchange. Will prompt for manual entry if none is active.")
                avg_price = st.number_input(
                    "Average Entry Price", 
                    value=0.0, 
                    min_value=0.0, 
                    step=0.01, 
                    key=f"manual_price_{alert_id}"
                )

            # Code preview
            ts_now = int(time.time())
            st.code(f"""-- Parameterized Adoption Insert Preview:
INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount, price, step, cycle_id, client_order_id, notes, created_at, updated_at)
VALUES ({target_bot_id}, 'adoption', 'filled', {abs(shortfall)}, {abs(shortfall)}, {avg_price}, 1, {cycle_id}, 'CQB_{target_bot_id}_ADOPTION_{ts_now}', '[MANUAL-ADOPTION] Adopted unowned exchange position.', {ts_now}, {ts_now});
""", language="sql")

            col1, col2 = st.columns([1, 4])
            with col1:
                if st.button("Approve Adoption", key=f"app_adopt_{alert_id}"):
                    if avg_price <= 0.0:
                        st.error("Cannot adopt: failed to fetch or supply a valid entry price from exchange. Try again.")
                    else:
                        conn_write = get_connection()
                        # Insert adoption order (parameterized)
                        conn_write.execute(
                            """INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount,
                               price, step, cycle_id, client_order_id, notes, created_at, updated_at)
                               VALUES (?, 'adoption', 'filled', ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                            (target_bot_id, abs(shortfall), abs(shortfall), avg_price, cycle_id,
                             f"CQB_{target_bot_id}_ADOPTION_{ts_now}",
                             "[MANUAL-ADOPTION] Adopted unowned exchange position.",
                             ts_now, ts_now)
                        )
                        # Mark alert as adopted (parameterized)
                        conn_write.execute(
                            "UPDATE unowned_position_alerts SET status = 'adopted', bot_id = ? WHERE id = ?",
                            (target_bot_id, alert_id)
                        )
                        conn_write.commit()
                        
                        # Reseal the trade state to update trades.open_qty
                        from engine.ledger import seal_trade_state
                        seal_trade_state(target_bot_id, force_recompute=True)
                        
                        st.success(f"Position successfully adopted for {bot_name}!")
                        time.sleep(0.5)
                        st.rerun()
            with col2:
                if st.button("Dismiss Alert", key=f"dismiss_{alert_id}"):
                    conn_write = get_connection()
                    conn_write.execute(
                        "UPDATE unowned_position_alerts SET status = 'dismissed' WHERE id = ?",
                        (alert_id,)
                    )
                    conn_write.commit()
                    st.info("Alert dismissed.")
                    time.sleep(0.5)
                    st.rerun()


def render_monitor_view():
    _notifications_fragment()

    # ── SINGLE AUTHORITATIVE HEALTH COMPUTATION ──────────────────────────────
    # Compute once per render cycle; all fragments read from session_state.
    # force_refresh is set to True when the user clicks 'Refresh Now'.
    try:
        _ex = get_exchange_instance(global_config.MARKET_TYPE)
        _health = _get_system_health(
            db_path=global_config.PATHS['DB_FILE'],
            exchange_instance=_ex,
            norm_fn=_norm_universal,
            qty_tolerance_fn=pair_qty_tolerance,
            force_refresh=st.session_state.pop("_force_health_refresh", False),
        )
        st.session_state["system_health_data"] = _health
    except Exception as _he:
        _health = st.session_state.get("system_health_data") or {}

    # ── STARTUP SUPPRESSION BANNER ───────────────────────────────────────────
    if _health.get("startup_suppression"):
        remaining = int(_health.get("startup_remaining_s", 0))
        st.warning(
            f"⏳ **Engine starting up** — health alerts suppressed for "
            f"**{remaining}s** remaining. Netting mismatches and order alerts "
            f"will not fire until the grace period expires."
        )

    render_unowned_positions_banner()

    col1, col2 = st.columns([4, 1])
    with col1:
        st.header("📊 Live Market Monitor")
        st.caption(f"Last Updated: {time.strftime('%H:%M:%S')} (Local)")
    with col2:
        if st.button("🔄 Pre-Flight Sync"):
            try:
                from engine.exchange_interface import ExchangeInterface
                from engine.database import update_active_positions_snapshot
                with st.spinner("Syncing exchange..."):
                    ex = ExchangeInterface()
                    pos = ex.fetch_positions()
                    update_active_positions_snapshot(pos)
                # Force health refresh so operator sees immediate updated state
                st.session_state["_force_health_refresh"] = True
                st.toast("✅ Active positions synchronized")
                time.sleep(0.5)
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")

    # --- Notifications (Phase 9.3) ---
    # Moved to _notifications_fragment() at the root of render_monitor_view()
    
    # --- Header Fragment (30 s refresh cycle, independent of bot grid) ---
    _header_metrics_fragment()

    # --- Auto-Refresh + Refresh Now (compact single row) ---
    _ar_col, _rn_col = st.columns([3, 1])
    with _ar_col:
        auto_refresh = st.toggle("⚡ Auto-Refresh (15s)", value=True, key="auto_refresh_toggle")
        # Only clear cache when user flips OFF→ON to force an immediate fresh load.
        if auto_refresh and not st.session_state.get("_prev_auto_refresh", True):
            st.session_state.pop("cached_monitor_data", None)
            st.session_state.pop("cached_header_data", None)
        st.session_state["_prev_auto_refresh"] = auto_refresh
    with _rn_col:
        if st.button("🔄 Refresh Now", width="stretch"):
            st.cache_data.clear()
            st.session_state["_force_health_refresh"] = True
            st.rerun()

    # Detect if the Reconciler / Forensic Wizard is actively in use.
    wizard_active = any(bool(st.session_state[k]) for k in st.session_state if k.startswith(("forensic_trades_", "adopt_force_sel_", "trade_sel_", "_confirm_")))


    # --- Chart Control Bar (symbol + timeframe drive data fetch + chart tab) ---
    try:
        conn_b = get_connection()
        cur_b = conn_b.cursor()
        cur_b.execute("SELECT id, name, pair FROM bots WHERE is_active = 1")
        active_bots_list = cur_b.fetchall()
    except:
        active_bots_list = []

    bot_options = ["None (Symbol View)"] + [f"{b[1]} ({b[2]})" for b in active_bots_list]

    # Compact 3-column control bar: [FocusBot + Symbol]  [Timeframe]
    # (Refresh Now button moved to the auto-refresh row above)
    _cb1, _cb2 = st.columns([3, 1])
    with _cb1:
        _c1a, _c1b = st.columns(2)
        with _c1a:
            selected_bot_str = st.selectbox("Focus Bot", bot_options, index=0, key="monitor_bot_select")

        target_symbol_list = list(global_config.ALLOWED_SYMBOLS)
        selected_bot_id = None

        if selected_bot_str != "None (Symbol View)":
            bot_name_sel = selected_bot_str.split(" (")[0]
            for b in active_bots_list:
                if b[1] == bot_name_sel:
                    selected_bot_id = b[0]
                    target_symbol_list = [b[2]] + [s for s in target_symbol_list if s != b[2]]
                    break
        else:
            active_pairs = list(set([b[2] for b in active_bots_list]))
            target_symbol_list = list(dict.fromkeys(active_pairs + target_symbol_list))

        with _c1b:
            symbol = st.selectbox("Symbol", target_symbol_list, key="monitor_symbol")
    with _cb2:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"], index=4, key="monitor_tf")

    # --- Data Fetching (Parallel) ---
    with st.spinner("Fetching market data..."):
        raw_orders = []
        # Parallel fetch for speed
        with ThreadPoolExecutor(max_workers=6) as executor:
            f_ohlcv = executor.submit(fetch_ohlcv_cached, global_config.MARKET_TYPE, symbol, timeframe)
            f_pos = executor.submit(fetch_positions_cached, global_config.MARKET_TYPE)
            f_bal = executor.submit(fetch_balance_cached, global_config.MARKET_TYPE)
            
            # 🚀 ORDER FETCH FIX: Fetch all generic orders at once to prevent 
            # CCXT rate limits and symbol mapping errors in parallel threads
            try:
                ex_inst = get_exchange_instance(global_config.MARKET_TYPE)
                # Setting symbol=None fetches all active orders for the whole account
                f_orders = executor.submit(ex_inst.fetch_open_orders, None)
            except Exception as e:
                print(f"Error submitting order fetch: {e}")
                f_orders = None
            
            ohlcv_data = f_ohlcv.result()
            
            if f_orders:
                try:
                    res = f_orders.result(timeout=10)
                    if res: raw_orders.extend(res)
                except Exception as e:
                    print(f"Failed to fetch market orders: {e}")
        
        # DEDUPLICATE ORDERS (Safety mechanism)
        market_orders = list({str(o.get('id', '')): o for o in raw_orders if o}.values())

    # --- UI Layout: Tabs ---
    tab_overview, tab_charts, tab_history = st.tabs(["📊 Overview", "📈 Live Charts", "📝 Orders & History"])

    with tab_overview:
        # --- FRAGMENTED OVERVIEW (v3.1.2) ---

        # Decorated with @st.fragment so this section refreshes independently
        # without triggering a full-page rerun.
        # -------------------------------------------------------------------------
        _bot_positions_fragment()
        _exchange_sync_diagnostics_fragment()

    with tab_charts:
        # --- Portfolio Risk Heatmap (Phase 10.2) — collapsed by default -------
        with st.expander("📊 Portfolio Risk Heatmap", expanded=False):
            try:
                import plotly.express as px
                conn = get_connection()
                df_risk = pd.read_sql(
                    "SELECT name, total_invested, current_step, avg_entry_price "
                    "FROM trades JOIN bots ON trades.bot_id = bots.id WHERE total_invested > 0",
                    conn
                )
                if not df_risk.empty:
                    fig_hm = px.treemap(
                        df_risk, path=['name'], values='total_invested',
                        color='current_step', color_continuous_scale='RdYlGn_r',
                        title="Risk Map (Size=Invested, Colour=Step/Risk)"
                    )
                    st.plotly_chart(fig_hm, width="stretch")
                else:
                    st.info("No active positions to display.")
            except Exception as _hm_err:
                st.error(f"Heatmap Error: {_hm_err}")

        st.subheader(f"📈 Live Market Chart: {symbol} ({timeframe})")
        if ohlcv_data:
            try:
                df_ohlcv = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_ohlcv['timestamp'] = pd.to_datetime(df_ohlcv['timestamp'], unit='ms')
                
                fig = go.Figure(data=[go.Candlestick(x=df_ohlcv['timestamp'],
                                open=df_ohlcv['open'],
                                high=df_ohlcv['high'],
                                low=df_ohlcv['low'],
                                close=df_ohlcv['close'],
                                name='Price')])
                
                # 🚀 UPGRADE 2: Visual Intelligence Overlays
                if selected_bot_id:
                    # Fetch bot entry/tp directly from DB — df_pos_f only exists inside
                    # the fragment scope and is not accessible here at the tab level.
                    try:
                        _conn_chart = get_connection()
                        _chart_row = _conn_chart.execute(
                            "SELECT t.avg_entry_price, t.open_qty "
                            "FROM trades t WHERE t.bot_id = ?",
                            (selected_bot_id,)
                        ).fetchone()
                        # Best available TP: look for a live TP order on exchange for this bot
                        _chart_prefix = f"CQB_{selected_bot_id}_"
                        _tp_orders = [o for o in market_orders
                                      if str(o.get('clientOrderId', '')).startswith(_chart_prefix)
                                      and 'TP' in str(o.get('clientOrderId', ''))]
                        be = float(_chart_row[0] or 0) if _chart_row else 0.0
                        tp = float(_tp_orders[0].get('price', 0)) if _tp_orders else 0.0
                    except Exception as _ce:
                        be, tp = 0.0, 0.0

                    # 1. Average Entry (Yellow Solid)
                    if be > 0:
                        fig.add_hline(y=be, line_dash="solid", line_color="#FFD700",
                                      annotation_text=f"ENTRY: {be:,.4f}",
                                      annotation_position="top left")

                    # 2. Take Profit (Green Solid — from live exchange order)
                    if tp > 0:
                        fig.add_hline(y=tp, line_dash="solid", line_color="#00FF00",
                                      annotation_text=f"TP: {tp:,.4f}",
                                      annotation_position="bottom right")

                    # 3. Active Grid/Safety Orders (Orange Dashed)
                    prefix = f"CQB_{selected_bot_id}_"
                    bot_orders_chart = [o for o in market_orders
                                        if str(o.get('clientOrderId', '')).startswith(prefix)]
                    grid_orders = [o for o in bot_orders_chart
                                   if 'GRID' in str(o.get('clientOrderId', ''))]
                    last_price = float(df_ohlcv['close'].iloc[-1])
                    grid_orders.sort(key=lambda x: abs(float(x.get('price', 0)) - last_price))
                    
                    # Limit to next 10 for readability as requested
                    for i, order in enumerate(grid_orders[:10]):
                        g_price = float(order.get('price', 0))
                        if g_price > 0:
                            fig.add_hline(y=g_price, line_dash="dash", line_color="#FFA500", 
                                          annotation_text=f"GRID {i+1}", 
                                          annotation_position="bottom left",
                                          opacity=0.6)

                fig.update_layout(
                    height=500,
                    margin=dict(l=10, r=10, t=30, b=10),
                    template="plotly_white",
                    xaxis_rangeslider_visible=False,
                    paper_bgcolor='rgba(0,0,0,0)',
                    plot_bgcolor='rgba(0,0,0,0)'
                )
                st.plotly_chart(fig)
                
            except Exception as e:
                st.error(f"Error rendering chart: {e}")
        else:
            st.warning(f"No market data available for {symbol} on {timeframe}.")

    with tab_history:
        # --- Open Orders Section (Live from Exchange) ---
        st.subheader("📋 Live Open Orders (from Exchange)")
        try:
             # Use the ALREADY FETCHED orders from parallel execution
             if market_orders:
                df_orders = pd.DataFrame(market_orders)
                
                # --- ENRICHMENT from DB ---
                try:
                    conn = get_connection()
                    # Fetch notes for open orders to explain "Why"
                    db_orders = pd.read_sql("SELECT client_order_id, notes FROM bot_orders WHERE status='open'", conn)
                    conn.close()
                    
                    if not db_orders.empty and 'clientOrderId' in df_orders.columns:
                        # Merge on Client Order ID
                        df_orders = df_orders.merge(db_orders, left_on='clientOrderId', right_on='client_order_id', how='left')
                        df_orders.rename(columns={'notes': 'Strategy/Logic'}, inplace=True)
                    else:
                        df_orders['Strategy/Logic'] = "N/A"
                except Exception as e:
                    pass # Fail silently on enrichment

                # Keep only relevant columns
                cols_to_keep = ['symbol', 'side', 'type', 'price', 'amount', 'Strategy/Logic', 'clientOrderId']
                df_orders = df_orders[[c for c in cols_to_keep if c in df_orders.columns]]
                
                # Formatter for price
                if 'price' in df_orders.columns:
                     df_orders['price'] = df_orders['price'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (float, int)) else x)
                     
                st.dataframe(
                    df_orders, 
                    width="stretch",
                    column_config={
                        "Strategy/Logic": st.column_config.TextColumn("Strategy/Logic", width="medium")
                    }
                )
             else:
                st.info("No open orders found on the exchange for active bot pairs.")
                
        except Exception as e:
            st.error(f"Could not load open orders: {e}")

        st.divider()

        # --- Recent Trade History (Added) ---
        st.subheader("📜 Recent Activity Log")
        st.caption("🧹 **Auto-Reconcile**: Normal startup cleanup of phantom ledger state & global flatten verification | ⚠️ **SYSTEM_WIPE**: Operator-initiated position wipe (requires review)")
        try:
            conn = get_connection()
            # Fetch last 200 actions for deep scrolling
            query_history = """
                SELECT 
                    datetime(timestamp, 'unixepoch', 'localtime') as Time,
                    action as Action,
                    symbol as Symbol,
                    price as Price,
                    amount as Amount,
                    pnl as 'Realized PnL',
                    notes as Details
                FROM trade_history 
                ORDER BY timestamp DESC 
                LIMIT 200
            """
            df_hist = pd.read_sql_query(query_history, conn)
            conn.close()
            
            if not df_hist.empty:
                # Distinguish Auto-Reconcile from actual operator SYSTEM_WIPEs
                def format_action(row):
                    act = row['Action']
                    details = str(row.get('Details') or '').lower()
                    if act == 'SYSTEM_WIPE':
                        # Default to loud SYSTEM_WIPE unless details confirm it was an automated background cleanup
                        is_auto = any(term in details for term in ['auto', 'reconcile', 'startup', 'zombie', 'ghost'])
                        is_manual = any(term in details for term in ['manual', 'operator', 'human', 'cleanup'])
                        if is_auto and not is_manual:
                            return '🧹 Auto-Reconcile'
                        else:
                            return '⚠️ SYSTEM_WIPE'
                    return act

                
                df_hist['Action'] = df_hist.apply(format_action, axis=1)

                # Format Price and PnL
                df_hist['Price'] = df_hist['Price'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else x)
                df_hist['Realized PnL'] = df_hist['Realized PnL'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) and x != 0 else "-")
                
                st.dataframe(
                    df_hist, 
                    width="stretch", 
                    hide_index=True,
                    column_config={
                        "Details": st.column_config.TextColumn("Details", width="large", help="Detailed logic/reasoning for this action")
                    }
                )
            else:
                st.caption("No trade history available yet.")
                
        except Exception as e:
            st.error(f"Error loading trade history: {e}")

    # The header and bot grid now refresh via native @st.fragment decorators.
    if auto_refresh and not wizard_active:
        st.caption(f"ℹ️ Fragments auto-updating every 15-30s. [Page Base Time: {time.strftime('%H:%M:%S')}]")
    elif wizard_active:
        st.warning(
            "⏸ **Auto-Refresh Paused** — Reconciler wizard is active. "
            "Refreshing now would wipe your in-progress recovery work."
        )
    else:
        st.caption("ℹ️ Tip: Auto-Refresh is OFF. Toggle it above for real-time updates.")