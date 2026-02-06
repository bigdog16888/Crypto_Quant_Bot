"""
Adopt Orphan Position Script

This script updates DB state to match exchange reality, splitting the orphan
position between the existing bots on that pair.

The bots will then calculate correct TPs and Grids based on the real position.
"""

from engine.database import get_connection, update_martingale_step
from engine.exchange_interface import ExchangeInterface

def adopt_orphan_btc():
    """Split exchange BTC position between Bot 41 and 43"""
    
    print("=" * 60)
    print("ADOPT ORPHAN: Syncing DB to Exchange Reality")
    print("=" * 60)
    
    # 1. Get exchange position
    ex = ExchangeInterface(market_type='future')
    positions = ex.exchange.fetch_positions()
    
    btc_pos = None
    for pos in positions:
        if 'BTC' in pos.get('symbol', ''):
            btc_pos = pos
            break
    
    if not btc_pos:
        print("No BTC position found on exchange!")
        return
    
    total_contracts = float(btc_pos.get('contracts', 0) or 0)
    entry_price = float(btc_pos.get('entryPrice', 0) or 0)
    
    print(f"\nExchange Position: {total_contracts} BTC @ ${entry_price:.2f}")
    print(f"Total Value: ${total_contracts * entry_price:.2f}")
    
    # 2. Get current DB state for Bot 41 and 43
    conn = get_connection()
    cur = conn.cursor()
    
    bot_41_qty = cur.execute('SELECT total_invested / avg_entry_price FROM trades WHERE bot_id = 41').fetchone()
    bot_43_qty = cur.execute('SELECT total_invested / avg_entry_price FROM trades WHERE bot_id = 43').fetchone()
    
    bot_41_qty = float(bot_41_qty[0]) if bot_41_qty and bot_41_qty[0] else 0
    bot_43_qty = float(bot_43_qty[0]) if bot_43_qty and bot_43_qty[0] else 0
    
    print(f"\nCurrent DB:")
    print(f"  Bot 41: {bot_41_qty:.6f} BTC")
    print(f"  Bot 43: {bot_43_qty:.6f} BTC")
    print(f"  Total:  {bot_41_qty + bot_43_qty:.6f} BTC")
    print(f"  Gap:    {total_contracts - (bot_41_qty + bot_43_qty):.6f} BTC")
    
    # 3. Split position evenly between both bots
    split_qty = total_contracts / 2
    split_value = split_qty * entry_price
    
    # Calculate step based on approximate grid fills
    # Assuming ~0.004 per grid, calculate how many fills got us here
    approx_grid_fills = int((split_qty - 0.003) / 0.004) if split_qty > 0.003 else 0
    step = min(approx_grid_fills, 10)  # Cap at step 10
    
    # Calculate TP (1.5% from entry)
    tp_price = entry_price * 1.015
    
    print(f"\nProposed Split:")
    print(f"  Bot 41: {split_qty:.6f} BTC, Step {step}, TP ${tp_price:.2f}")
    print(f"  Bot 43: {split_qty:.6f} BTC, Step {step}, TP ${tp_price:.2f}")
    
    # 5. Update both bots
    update_martingale_step(41, step, split_value, entry_price, tp_price)
    update_martingale_step(43, step, split_value, entry_price, tp_price)
    update_martingale_step(43, step, split_value, entry_price, tp_price)
    
    print("\n✅ Updated both bots to match exchange reality!")
    print(f"  Bot 41: ${split_value:.2f} @ ${entry_price:.2f}, Step {step}")
    print(f"  Bot 43: ${split_value:.2f} @ ${entry_price:.2f}, Step {step}")
    print(f"\nNext steps:")
    print(f"  1. When bot runs, it will calculate next grid based on step {step}")
    print(f"  2. TP orders will be placed at ${tp_price:.2f}")
    print(f"  3. When TP hits, both bots reset to IDLE")


if __name__ == "__main__":
    adopt_orphan_btc()
