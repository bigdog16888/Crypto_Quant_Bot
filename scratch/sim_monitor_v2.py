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
        if c_step == 0 and "EXITING" not in str(row_bot.get('status','')).upper():
            hedge_amounts[b_id] = 0.0
            continue
        h_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge')]['filled_amount'].sum()
        hx_sum = df_h[(df_h['bot_id'] == b_id) & (df_h['order_type'] == 'hedge_tp')]['filled_amount'].sum()
        hedge_amounts[b_id] = max(0.0, float(h_sum - hx_sum))

    virtual_qty_by_pair = {}
    pair_prices = {}
    for _, row in df_pos.iterrows():
        open_qty_v = float(row['open_qty'] or 0)
        invested = float(row['total_invested'] or 0)
        avg_price = float(row['avg_entry_price'] or 0)
        bot_id = row['id']
        pair = row['pair'].split(':')[0].replace('/', '').replace(':', '').upper()
        if not pair.endswith('USDC') and not pair.endswith('USDT'): pair += 'USDC'
        side = str(row['direction']).upper()
        qty_abs = open_qty_v if open_qty_v > 0 else (invested / avg_price if avg_price > 0 else 0)
        if pair not in pair_prices or pair_prices[pair] == 1.0: pair_prices[pair] = avg_price if avg_price > 0 else 1.0
        h_qty = hedge_amounts.get(bot_id, 0.0)
        effective_qty = qty_abs - h_qty
        virtual_qty_by_pair[(pair, side)] = virtual_qty_by_pair.get((pair, side), 0.0) + effective_qty

    df_physical = pd.read_sql("SELECT pair, side, size, entry_price FROM active_positions", conn)
    physical_qty_by_pair = {}
    for _, row in df_physical.iterrows():
        qty = abs(float(row['size']))
        side = 'LONG' if str(row['side']).upper() in ('BUY', 'LONG') else 'SHORT'
        p = str(row['pair']).upper().replace('/', '').replace(':', '')
        physical_qty_by_pair[(p, side)] = physical_qty_by_pair.get((p, side), 0.0) + qty
        if p not in pair_prices: pair_prices[p] = float(row['entry_price'])

    total_v_usd = 0.0
    total_p_usd = 0.0
    all_symbols = set([k[0] for k in virtual_qty_by_pair.keys()]) | set([k[0] for k in physical_qty_by_pair.keys()])
    for p in sorted(all_symbols):
        v_net = virtual_qty_by_pair.get((p, 'LONG'), 0.0) - virtual_qty_by_pair.get((p, 'SHORT'), 0.0)
        p_net = physical_qty_by_pair.get((p, 'LONG'), 0.0) - physical_qty_by_pair.get((p, 'SHORT'), 0.0)
        price = pair_prices.get(p, 1.0)
        total_v_usd += v_net * price
        total_p_usd += p_net * price
        if abs(p_net - v_net) > 0.001:
             print(f"Mismatch: {p} | System Net Qty: {v_net:+.4f} | Exchange Net Qty: {p_net:+.4f} | Diff USD: {(p_net - v_net)*price:,.2f}")

    print(f"System Net USD: ${total_v_usd:,.2f}")
    print(f"Exchange Net USD: ${total_p_usd:,.2f}")
    print(f"Global Diff: ${total_p_usd - total_v_usd:,.2f}")
    conn.close()

if __name__ == "__main__":
    simulate_monitor()
