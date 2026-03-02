import os
import json
import sqlite3
from dotenv import load_dotenv

load_dotenv()
from engine.exchange_interface import ExchangeInterface, normalize_symbol

def fix_notional_gaps():
    print("Fetching exchange positions...")
    ex = ExchangeInterface('future')
    pos = ex.fetch_positions()
    
    phys_notional = {}
    for p in pos:
        sym = normalize_symbol(p['symbol'])
        size = float(p['contracts'])
        price = float(p['entryPrice'])
        val = abs(size * price)
        if val > 0:
            phys_notional[sym] = {'size': size, 'value': val, 'price': price}

    conn = sqlite3.connect("crypto_bot.db")
    c = conn.cursor()
    
    # Get active virtual investments per pair
    c.execute("""
        SELECT b.id, b.pair, b.name, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE t.total_invested > 0 AND b.is_active = 1
    """)
    bots = c.fetchall()
    
    virtual_by_pair = {}
    for bot in bots:
        bot_id, pair, name, invested, avg_price = bot
        sym = normalize_symbol(pair)
        if sym not in virtual_by_pair:
            virtual_by_pair[sym] = {'total_virtual': 0.0, 'bots': []}
        
        virtual_by_pair[sym]['total_virtual'] += float(invested)
        virtual_by_pair[sym]['bots'].append(bot)

    print("\n--- NOTIONAL GAP ANALYSIS ---")
    for sym, v_data in virtual_by_pair.items():
        virt = v_data['total_virtual']
        phys = phys_notional.get(sym, {}).get('value', 0.0)
        
        print(f"[{sym}] Virtual: ${virt:.2f} | Physical: ${phys:.2f}")
        
        if virt > 10.0 and phys > 1.0 and abs(virt - phys) > 10.0:
            if virt > phys:
                print(f"   -> GAP DETECTED: Virtual is higher by ${virt - phys:.2f}. Scaling down bots.")
                scale_factor = phys / virt
                
                import time
                for bot in v_data['bots']:
                    bot_id, pair, name, invested, avg_price = bot
                    new_invested = invested * scale_factor
                    
                    print(f"      - {name} ({bot_id}): ${invested:.2f} -> ${new_invested:.2f}")
                    c.execute("UPDATE trades SET total_invested = ? WHERE bot_id = ?", (new_invested, bot_id))
                    
                    # Log to trade history so it's documented
                    c.execute("""
                        INSERT INTO trade_history (bot_id, timestamp, action, amount, price, step, pnl, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (bot_id, int(time.time()), 'MANUAL_GAP_SYNC', invested - new_invested, 0, 0, 0, f"Scaled down investments to match physical exchange data. Multiplier: {scale_factor:.4f}"))
            
    conn.commit()
    conn.close()
    print("\n✅ Database realignment complete!")

if __name__ == '__main__':
    fix_notional_gaps()
