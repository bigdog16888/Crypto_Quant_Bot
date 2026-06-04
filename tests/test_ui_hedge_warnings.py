import pytest
import pandas as pd
import json

def test_ui_hedge_warnings_suppression():
    # 1. Simulate the data structure returned by _fetch_fresh_monitor_data
    # df_pos_f has columns: id, status, total_invested, bot_type, parent_bot_id, parent_name, cycle_phase, current_step
    df_pos_f = pd.DataFrame([
        {
            'id': 10017,
            'name': "xrp long",
            'status': "🔴 IN TRADE | Step 6",
            'bot_type': 'standard',
            'parent_bot_id': None,
            'parent_name': None,
            'total_invested': 812.62,
            'cycle_phase': 'ACTIVE',
            'current_step': 6,
            'direction': 'LONG',
            'pair': 'XRP/USDC:USDC',
            'config': '{}'
        },
        {
            'id': 100313,
            'name': "xrp long_hedge",
            'status': "🔴 HEDGE ACTIVE | Step 59",
            'bot_type': 'hedge_child',
            'parent_bot_id': 10017,
            'parent_name': "xrp long",
            'total_invested': 912.13,
            'cycle_phase': 'ACTIVE',
            'current_step': 59,
            'direction': 'SHORT',
            'pair': 'XRP/USDC:USDC',
            'config': '{}'
        }
    ])

    # 2. Simulate 0 physical orders for the child bot
    physical_order_counts = {10017: 1, 100313: 0}

    # 3. Simulate highlight_health logic
    def highlight_health(row):
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
            return f"⚠️ {status}"
        return status

    # 4. Verify that parent (with 1 order) has status unmodified
    parent_row = df_pos_f.iloc[0]
    parent_res = highlight_health(parent_row)
    assert parent_res == "🔴 IN TRADE | Step 6"

    # 5. Verify that active hedge child (with 0 orders but parent in trade) has warning SUPPRESSED
    child_row = df_pos_f.iloc[1]
    child_res = highlight_health(child_row)
    assert "⚠️" not in child_res
    assert child_res == "🔴 HEDGE ACTIVE | Step 59"

    # 6. Verify that if parent was NOT in trade (e.g. Scanning), the warning is NOT suppressed
    df_pos_f.loc[0, 'status'] = "🟢 SCANNING"
    child_res_warned = highlight_health(child_row)
    assert child_res_warned == "⚠️ 🔴 HEDGE ACTIVE | Step 59"


def test_ui_hedge_trigger_info():
    # Simulate df_pos_f again
    df_pos_f = pd.DataFrame([
        {
            'id': 10017,
            'name': "xrp long",
            'status': "🔴 IN TRADE | Step 6",
            'bot_type': 'standard',
            'parent_bot_id': None,
            'parent_name': None,
            'total_invested': 812.62,
            'cycle_phase': 'ACTIVE',
            'current_step': 6,
            'direction': 'LONG',
            'pair': 'XRP/USDC:USDC',
            'config': '{}'
        },
        {
            'id': 100313,
            'name': "xrp long_hedge",
            'status': "🔴 HEDGE ACTIVE | Step 59",
            'bot_type': 'hedge_child',
            'parent_bot_id': 10017,
            'parent_name': "xrp long",
            'total_invested': 912.13,
            'cycle_phase': 'ACTIVE',
            'current_step': 59,
            'direction': 'SHORT',
            'pair': 'XRP/USDC:USDC',
            'config': '{}'
        }
    ])

    market_orders_f = []
    pair_prices = {'XRPUSDC': 1.3225}

    def _norm_universal(pair):
        if not pair: return ""
        return pair.split(':')[0].replace('/', '').upper()

    def extract_info(row):
        res = {
            'Trigger': 'N/A', 'Orders': '0', 'TP_Price': 0.0,
            'Grid_Price': 0.0, 'Grid_Amount': 0.0,
            'Expected_Profit': 0.0, 'EE_Status': '-',
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
            pair_key = _norm_universal(row.get('pair', ''))
            current_price = _clean(pair_prices.get(pair_key, 0.0))
            
            inv = _clean(row.get('total_invested'))
            is_in_trade = inv > 0.01 or str(row.get('cycle_phase', '')).upper() == 'ACTIVE'

            # Trigger Description / Order Tracking
            parts = []
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
        except Exception as e:
            res['Trigger'] = f"ERR: {e}"
        return res

    # 1. Parent is in trade, child has no orders: child should display parent awaiting message
    child_info = extract_info(df_pos_f.iloc[1])
    assert child_info['Trigger'] == "Awaiting parent 'xrp long' exit"

    # 2. Parent is NOT in trade: child should show "⚠️ NO ORDERS"
    df_pos_f.loc[0, 'status'] = "🟢 SCANNING"
    child_info_no_parent = extract_info(df_pos_f.iloc[1])
    assert child_info_no_parent['Trigger'] == "⚠️ NO ORDERS"


def test_ui_hedge_child_ee_status():
    # Test that a hedge child bot always displays '⏳ Parent TP pending' under EE_Status
    # when it is in trade, while standard bots show their decay/time status.
    import time
    
    # 1. Setup mock data
    df_pos_f = pd.DataFrame([
        {
            'id': 10017,
            'name': "xrp long",
            'status': "🔴 IN TRADE | Step 6",
            'bot_type': 'standard',
            'parent_bot_id': None,
            'parent_name': None,
            'total_invested': 812.62,
            'cycle_phase': 'ACTIVE',
            'current_step': 6,
            'direction': 'LONG',
            'pair': 'XRP/USDC:USDC',
            'config': '{"UseEarlyExit": true, "EEStartHours": 2, "DecayIntervalMins": 15, "DecayPercentPerInterval": 10}',
            'basket_start_time': time.time() - 3600 * 3 # 3 hours ago (exceeded EEStartHours of 2)
        },
        {
            'id': 100313,
            'name': "xrp long_hedge",
            'status': "🔴 HEDGE ACTIVE | Step 59",
            'bot_type': 'hedge_child',
            'parent_bot_id': 10017,
            'parent_name': "xrp long",
            'total_invested': 912.13,
            'cycle_phase': 'ACTIVE',
            'current_step': 59,
            'direction': 'SHORT',
            'pair': 'XRP/USDC:USDC',
            'config': '{"UseEarlyExit": true, "EEStartHours": 2, "DecayIntervalMins": 15, "DecayPercentPerInterval": 10}',
            'basket_start_time': time.time() - 3600 * 3
        }
    ])

    market_orders_f = []
    pair_prices = {'XRPUSDC': 1.3225}

    def _norm_universal(pair):
        if not pair: return ""
        return pair.split(':')[0].replace('/', '').upper()

    def extract_info(row):
        res = {
            'Trigger': 'N/A', 'Orders': '0', 'TP_Price': 0.0,
            'Grid_Price': 0.0, 'Grid_Amount': 0.0,
            'Expected_Profit': 0.0, 'EE_Status': '-',
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
            pair_key = _norm_universal(row.get('pair', ''))
            current_price = _clean(pair_prices.get(pair_key, 0.0))
            
            inv = _clean(row.get('total_invested'))
            is_in_trade = inv > 0.01 or str(row.get('cycle_phase', '')).upper() == 'ACTIVE'

            # Early Exit (EE) Status
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
        except Exception as e:
            res['EE_Status'] = f"ERR: {e}"
        return res

    # 2. Extract info
    parent_info = extract_info(df_pos_f.iloc[0])
    child_info = extract_info(df_pos_f.iloc[1])

    # 3. Assertions
    # Parent (standard bot) should show heat/decay since it exceeded EEStartHours
    assert "🔥" in parent_info['EE_Status']
    # Child (hedge child bot) should show parent TP pending
    assert child_info['EE_Status'] == "⏳ Parent TP pending"
