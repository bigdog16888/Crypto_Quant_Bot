"""
Emergency repair for SOL and Gold bots:
- Their bot_orders history from reset_cleared cycles is unreliable (adoption_reduce wipes net)
- Physical position exists on exchange (4.06 SOL, -0.176 gold)
- Fetch entryPrice directly from exchange and write into trades

This is identical to what PASS 3 of adopt_from_physical_positions does,
but run manually so we don't have to wait for the 60-cycle periodic trigger.
"""
import sys, time; sys.path.insert(0, '.')
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface
import json, config.settings

cfg = config.settings.config
conn = get_connection()

# Get bot details
target_bots = conn.execute("""
    SELECT b.id, b.pair, b.direction, b.base_size, b.config,
           t.cycle_id, t.current_step, t.basket_start_time
    FROM bots b JOIN trades t ON b.id = t.bot_id
    WHERE b.id IN (10008, 10019)  -- SOL LONG, XAU SHORT
""").fetchall()

# Initialize exchange
demo_cfg = cfg.TRADING_CONFIG.copy() if hasattr(cfg, 'TRADING_CONFIG') else {}
market_type = cfg.MARKET_TYPE if hasattr(cfg, 'MARKET_TYPE') else 'future'

try:
    ex = ExchangeInterface(market_type=market_type)
    positions = ex.fetch_positions()
    print(f"Fetched {len(positions)} positions from exchange")

    for r in target_bots:
        bot_id, pair, direction, base_size, config_json, cycle_id, step, bst = r
        print(f"\n--- Bot {bot_id} {pair} {direction} ---")

        # Find matching position
        from engine.exchange_interface import normalize_symbol
        phys = next((p for p in positions if normalize_symbol(p.get('symbol','')) == normalize_symbol(pair)), None)

        if not phys:
            print(f"  WARNING: No physical position found for {pair} on exchange!")
            continue

        phys_qty = float(phys.get('contracts', 0) or 0)
        entry_price = float(phys.get('entryPrice', 0) or 0)
        phys_side = 'LONG' if phys_qty > 0 else 'SHORT'

        print(f"  Exchange: qty={phys_qty} entry={entry_price} side={phys_side}")
        print(f"  Bot direction: {direction}")

        if phys_side.upper() != direction.upper():
            print(f"  WARNING: Direction mismatch! Bot={direction} Exchange={phys_side}")
            continue

        if abs(phys_qty) < 0.0001 or entry_price <= 0:
            print(f"  WARNING: Invalid position data (qty={phys_qty} price={entry_price})")
            continue

        phys_invested = round(abs(phys_qty) * entry_price, 4)
        true_step = max(step, 1)
        new_bst = bst if bst and bst > 0 else int(time.time())

        print(f"  Applying: invested={phys_invested:.4f} avg={entry_price:.4f} step={true_step}")

        conn.execute("""
            UPDATE trades
            SET total_invested=?, avg_entry_price=?, current_step=?,
                entry_confirmed=1, basket_start_time=?,
                cycle_id=COALESCE(cycle_id, 1)
            WHERE bot_id=?
        """, (phys_invested, entry_price, true_step, new_bst, bot_id))
        
        _synthetic_cid = f"CQB_{bot_id}_PASS3_{int(time.time()*1000)}"
        _synthetic_oid = f"PASS3_ORPHAN_{bot_id}_{int(time.time())}"
        conn.execute("""
            INSERT OR IGNORE INTO bot_orders
              (bot_id, order_id, client_order_id, order_type, price, amount,
               filled_amount, status, step, cycle_id, created_at, updated_at)
            VALUES (?, ?, ?, 'adoption', ?, ?, ?, 'filled', ?, COALESCE((SELECT cycle_id FROM trades WHERE bot_id=?), 1), ?, ?)
        """, (
            bot_id, _synthetic_oid, _synthetic_cid,
            entry_price, abs(phys_qty), abs(phys_qty),
            true_step, bot_id, 
            int(time.time()), int(time.time())
        ))
        
        conn.commit()
        print(f"  ✅ Updated trades for bot {bot_id}")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback; traceback.print_exc()
finally:
    conn.close()

print("\nDone.")
