import sys
import os
import sqlite3
import json
import time
from datetime import datetime

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

def get_db_connection():
    return sqlite3.connect('crypto_bot.db')

def analyze_bots():
    print("="*80)
    print("🕵️ DEEP AUDIT: TRIGGERS, ORDERS & POSITIONS")
    print("="*80)

    # 1. Setup Exchange
    try:
        exchange = ExchangeInterface(market_type='future')
        print("✅ Exchange Connected")
    except Exception as e:
        print(f"❌ Exchange Connection Failed: {e}")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    # 2. Get IN TRADE Bots
    cursor.execute("""
        SELECT b.id, b.name, b.pair, b.strategy_type, b.config, 
               t.total_invested, t.entry_order_id, t.basket_start_time, t.current_step
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.status = 'IN TRADE'
    """)
    bots = cursor.fetchall()
    
    print(f"\nFound {len(bots)} bots currently 'IN TRADE' in Database.")

    # 3. Analyze Each Bot
    real_positions = 0
    ghost_positions = 0
    
    # Pre-fetch all open orders for efficiency
    print("   Fetching all open orders...")
    all_open_orders = exchange.fetch_open_orders()
    # Map by ClientID prefix for fast lookup
    orders_by_bot = {}
    for o in all_open_orders:
        cid = o.get('clientOrderId', '')
        # Format: CQB_{bot_id}_...
        if cid.startswith('CQB_'):
            parts = cid.split('_')
            if len(parts) >= 2:
                bid = parts[1]
                if bid not in orders_by_bot: orders_by_bot[bid] = []
                orders_by_bot[bid].append(o)

    print("-" * 80)
    print(f"{'BOT ID':<6} | {'PAIR':<10} | {'ENTRY ID':<12} | {'ENTRY STATUS':<12} | {'TP/GRID':<10} | {'VERDICT':<10}")
    print("-" * 80)

    for bot in bots:
        bid, name, pair, strat, cfg_json, invested, entry_oid, start_time, step = bot
        bid_str = str(bid)
        
        # A. Check Entry Order Status
        entry_status = "UNKNOWN"
        if entry_oid:
            try:
                # Check if we can find it in history/open
                # Just fetch it directly to be sure
                # (Note: heavy on API, but necessary for deep audit)
                try:
                    ord_obj = exchange.fetch_order(entry_oid, pair)
                    entry_status = ord_obj['status'].upper()
                except Exception as e:
                    if "Order not found" in str(e):
                        entry_status = "MISSING"
                    else:
                        entry_status = "ERR"
            except:
                entry_status = "ERR"
        else:
            entry_status = "NONE"

        # B. Check Open Orders (TP/Grid)
        bot_orders = orders_by_bot.get(bid_str, [])
        tp_count = len([o for o in bot_orders if '_TP_' in o.get('clientOrderId', '')])
        grid_count = len([o for o in bot_orders if '_GRID_' in o.get('clientOrderId', '')])
        
        # C. Verdict
        is_ghost = False
        if entry_status in ['MISSING', 'CANCELED', 'REJECTED']:
            is_ghost = True
            verdict = "👻 GHOST"
            ghost_positions += 1
        elif entry_status == 'FILLED':
            verdict = "✅ REAL"
            real_positions += 1
        elif entry_status == 'OPEN':
            verdict = "⏳ ENTERING"
        else:
            verdict = "❓ UNKNOWN"
            
        print(f"{bid:<6} | {pair:<10} | {str(entry_oid)[:10] if entry_oid else 'None':<12} | {entry_status:<12} | {tp_count}/{grid_count:<9} | {verdict:<10}")
        
        # D. Trigger Reason (Quick check of config)
        # Parse config to see if it looks aggressive
        try:
            cfg = json.loads(cfg_json)
            # print(f"       -> Config: RSI={cfg.get('rsi_limit')}, Base=${cfg.get('base_size')}")
        except: pass

    print("-" * 80)
    print(f"SUMMARY: {real_positions} Real Positions, {ghost_positions} Ghost Positions.")
    
    # 4. Exchange Position Check
    print("\n⚖️  NET POSITION CHECK")
    positions = exchange.fetch_positions()
    for p in positions:
        sz = float(p.get('contracts', 0) or p.get('size', 0) or 0)
        if sz != 0:
            print(f"   Exchange has {p['symbol']}: {sz} contracts")

if __name__ == "__main__":
    analyze_bots()
