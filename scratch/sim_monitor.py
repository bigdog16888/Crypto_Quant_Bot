import sqlite3
import pandas as pd
import json

DB_PATH = "crypto_bot.db"

def simulate_monitor():
    conn = sqlite3.connect(DB_PATH)
    
    # 1. Fetch Bots (df_pos)
    query_all = """
        SELECT b.id, b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price, b.is_active, b.status, t.open_qty
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
    """
    df_pos = pd.read_sql(query_all, conn)
    
    # 2. Fetch Hedges
    query_h = """
        SELECT bo.bot_id, bo.order_type, bo.filled_amount
        FROM bot_orders bo
        JOIN bots b ON bo.bot_id = b.id
        WHERE b.is_active = 1
          AND bo.order_type IN ('hedge', 'hedge_tp')
          AND bo.status IN ('filled', 'closed', 'auto_closed', 'hedge_exited')
    """
    df_h = pd.read_sql(query_h, conn)
    
    hedge_amounts = {}
    for b_id in df_pos['id'].unique():
        row_bot = df_pos[df_pos['id'] == b_id].iloc[0]
        c_step = int(row_bot.get('current_step', 0) or 0)
        
        # Monitor logic: skip hedges if step is 0 and not exiting
        if c_step == 0 and "EXITING" not in str(row_bot.get('status','')).upper():
            hedge_amounts[b_id] = 0.0
            continue

        h_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge')]['filled_amount'].sum()
        hx_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge_tp')]['filled_amount'].sum()
        hedge_amounts[b_id] = max(0.0, float(h_sum - hx_sum))

    # 3. Virtual Net
    virtual_qty_by_pair = {}
    pair_prices = {}
    
    for _, row in df_pos.iterrows():
        open_qty_v = float(row['open_qty'] or 0)
        invested = float(row['total_invested'] or 0)
        avg_price = float(row['avg_entry_price'] or 0)
        bot_id = row['id']
        pair = row['pair'].split(':')[0].replace('/', '').upper()
        side = str(row['direction']).upper()
        
        if open_qty_v > 0:
            qty_abs = open_qty_v
            ref_price = avg_price if avg_price > 0 else 1.0
        elif invested > 0 and avg_price > 0:
            qty_abs = invested / avg_price
            ref_price = avg_price
        else:
            qty_abs = 0
            ref_price = 1.0
        
        if pair not in pair_prices or pair_prices[pair] == 1.0:
            pair_prices[pair] = ref_price
            
        h_qty = hedge_amounts.get(bot_id, 0.0)
        effective_qty = qty_abs - h_qty
        
        composite_key = (pair, side)
        virtual_qty_by_pair[composite_key] = virtual_qty_by_pair.get(composite_key, 0.0) + effective_qty

    # 4. Physical Net
    df_physical = pd.read_sql("SELECT pair, side, size, entry_price FROM active_positions", conn)
    physical_qty_by_pair = {}
    for _, row in df_physical.iterrows():
        qty = abs(float(row['size']))
        side = 'LONG' if str(row['side']).upper() in ('BUY', 'LONG') else 'SHORT'
        pair = str(row['pair']).upper().replace('/', '').replace('USDC', '') + 'USDC'
        pair = pair.replace(':', '')
        
        # Universal normalization (simplified)
        p_key = pair.replace('USDC', '').replace(':', '') + 'USDC'
        
        composite_key = (p_key, side)
        physical_qty_by_pair[composite_key] = physical_qty_by_pair.get(composite_key, 0.0) + qty
        if p_key not in pair_prices:
            pair_prices[p_key] = float(row['entry_price'])

    # 5. Compare
    results = []
    all_symbols = set([k[0] for k in virtual_qty_by_pair.keys()]) | set([k[0] for k in physical_qty_by_pair.keys()])
    
    total_v_usd = 0.0
    total_p_usd = 0.0
    
    for p in sorted(all_symbols):
        v_l = virtual_qty_by_pair.get((p, 'LONG'), 0.0)
        v_s = virtual_qty_by_pair.get((p, 'SHORT'), 0.0)
        v_net = v_l - v_s
        
        p_l = physical_qty_by_pair.get((p, 'LONG'), 0.0)
        p_s = physical_qty_by_pair.get((p, 'SHORT'), 0.0)
        p_net = p_l - p_s
        
        price = pair_prices.get(p, 1.0)
        
        total_v_usd += v_net * price
        total_p_usd += p_net * price
        
        results.append({
            "Symbol": p,
            "Virtual Net Qty": v_net,
            "Physical Net Qty": p_net,
            "Price": price,
            "Diff USD": (p_net - v_net) * price
        })

    print(json.dumps({
        "summary": {
            "System Net USD": total_v_usd,
            "Exchange Net USD": total_p_usd,
            "Diff USD": total_p_usd - total_v_usd
        },
        "details": results,
        "hedge_amounts": {str(k): v for k, v in hedge_amounts.items() if v > 0}
    }, indent=2))
    conn.close()

if __name__ == "__main__":
    simulate_monitor()
