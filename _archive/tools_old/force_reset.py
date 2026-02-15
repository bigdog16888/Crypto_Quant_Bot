"""
Force Reset Utility for Crypto Bot

Use this when:
1. Positions closed on exchange but DB still shows "in trade"
2. Need to manually clean up stale bot state
3. Testnet positions expired/liquidated

Usage:
  python tools/force_reset.py --list           # Show all bots with stale positions
  python tools/force_reset.py --bot-id 32      # Reset specific bot
  python tools/force_reset.py --all            # Reset ALL stale bots (USE WITH CAUTION)
  python tools/force_reset.py --verify         # Check exchange positions vs DB state
"""

import sys
import os
import argparse
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, reset_bot_after_tp, log_trade, get_bot_status
from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ForceReset")


def get_stale_bots():
    """Find bots that think they're in trade but have no exchange position."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all active bots with invested > 0
    cursor.execute('''
        SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1 AND t.total_invested > 0
    ''')
    
    in_trade_bots = cursor.fetchall()
    conn.close()
    
    if not in_trade_bots:
        return []
    
    # Check exchange for actual positions
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.exchange.fetch_positions()
        
        # Build map of positions by symbol
        position_map = {}
        for pos in positions:
            if pos and float(pos.get('contracts', 0)) != 0:
                sym = pos.get('symbol')
                position_map[sym] = {
                    'size': float(pos['contracts']),
                    'side': pos.get('side'),
                    'entry': float(pos.get('entryPrice', 0))
                }
    except Exception as e:
        logger.error(f"Could not fetch exchange positions: {e}")
        return []
    
    stale = []
    for bot in in_trade_bots:
        bot_id, name, pair, direction, invested, entry = bot
        
        # Check if exchange has position for this pair
        has_position = pair in position_map
        
        if not has_position:
            stale.append({
                'id': bot_id,
                'name': name,
                'pair': pair,
                'direction': direction,
                'invested': invested,
                'entry': entry,
                'reason': 'No position on exchange'
            })
        else:
            # Position exists - verify direction matches
            pos = position_map[pair]
            expected_side = 'long' if direction == 'LONG' else 'short'
            if pos['side'] and pos['side'].lower() != expected_side:
                # Mismatched direction - could be a different bot or hedged
                pass  # Don't flag as stale, could be intentional
    
    return stale


def reset_bot(bot_id: int, reason: str = "Manual force reset"):
    """Force reset a bot to IDLE state."""
    try:
        status = get_bot_status(bot_id)
        if not status:
            logger.error(f"Bot {bot_id} not found")
            return False
        
        name, pair, step, invested, entry, tp, last_exit, last_exit_time = status
        
        if invested <= 0:
            logger.info(f"Bot {name} is already IDLE (invested={invested})")
            return True
        
        # Log the force reset
        log_trade(
            bot_id=bot_id,
            action='FORCE_RESET',
            symbol=pair,
            price=0,
            amount=invested / entry if entry > 0 else 0,
            cost_usdc=invested,
            order_id='FORCE_RESET',
            step=step,
            pnl=0,
            notes=f"Force Reset: {reason}"
        )
        
        # Reset the bot state
        reset_bot_after_tp(bot_id, exit_price=0)
        
        logger.info(f"✅ Bot {name} (ID: {bot_id}) reset to IDLE. Was: ${invested:.2f} @ {entry:.2f}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to reset bot {bot_id}: {e}")
        return False


def verify_state():
    """Compare exchange positions with DB state."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all active bots
    cursor.execute('''
        SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
        ORDER BY b.pair, b.name
    ''')
    
    bots = cursor.fetchall()
    conn.close()
    
    # Get exchange positions
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.exchange.fetch_positions()
        
        position_map = {}
        for pos in positions:
            if pos and float(pos.get('contracts', 0)) != 0:
                sym = pos.get('symbol')
                position_map[sym] = {
                    'size': float(pos['contracts']),
                    'side': pos.get('side'),
                    'entry': float(pos.get('entryPrice', 0)),
                    'notional': float(pos.get('notional', 0) or 0)
                }
    except Exception as e:
        logger.error(f"Could not fetch exchange positions: {e}")
        return
    
    print("\n" + "=" * 80)
    print("STATE VERIFICATION REPORT")
    print("=" * 80)
    
    print(f"\n📊 Exchange Positions ({len(position_map)} active):")
    for sym, pos in position_map.items():
        print(f"  {sym}: {pos['side']} | Size: {pos['size']} | Entry: {pos['entry']:.2f} | Notional: ${abs(pos['notional']):.2f}")
    
    print(f"\n🤖 Bot States ({len(bots)} bots):")
    
    # Group by pair
    by_pair = {}
    for bot in bots:
        pair = bot[2]
        if pair not in by_pair:
            by_pair[pair] = []
        by_pair[pair].append(bot)
    
    for pair, pair_bots in by_pair.items():
        exchange_pos = position_map.get(pair)
        print(f"\n  {pair}:")
        
        if exchange_pos:
            print(f"    Exchange: {exchange_pos['side']} | Size: {exchange_pos['size']} | ${abs(exchange_pos['notional']):.2f}")
        else:
            print(f"    Exchange: NO POSITION ⚠️")
        
        total_long = 0
        total_short = 0
        
        for bot in pair_bots:
            bot_id, name, _, direction, invested, entry = bot
            status = "IN TRADE" if invested > 0 else "IDLE"
            status_icon = "🔴" if invested > 0 and not exchange_pos else "🟢" if invested > 0 else "⚪"
            
            if invested > 0:
                if direction == 'LONG':
                    total_long += invested / entry if entry > 0 else 0
                else:
                    total_short += invested / entry if entry > 0 else 0
            
            print(f"    {status_icon} {name}: {status} | {direction} | ${invested:.2f}")
        
        # Net position
        net = total_long - total_short
        print(f"    Net Virtual Position: {net:.6f} ({'LONG' if net > 0 else 'SHORT' if net < 0 else 'FLAT'})")
        
        # Warning if mismatch
        if exchange_pos is None and (total_long > 0 or total_short > 0):
            print(f"    ⚠️  STALE: Bots think they're in trade but NO exchange position!")
    
    print("\n" + "=" * 80)


def main():
    parser = argparse.ArgumentParser(description='Force Reset Utility for Crypto Bot')
    parser.add_argument('--list', action='store_true', help='List all stale bots')
    parser.add_argument('--bot-id', type=int, help='Reset specific bot by ID')
    parser.add_argument('--all', action='store_true', help='Reset ALL stale bots')
    parser.add_argument('--verify', action='store_true', help='Verify exchange vs DB state')
    
    args = parser.parse_args()
    
    if args.verify:
        verify_state()
        return
    
    if args.list or args.all:
        stale = get_stale_bots()
        
        if not stale:
            print("✅ No stale bots found. All bots are properly synced.")
            return
        
        print(f"\n⚠️  Found {len(stale)} stale bot(s):\n")
        for bot in stale:
            print(f"  ID: {bot['id']} | {bot['name']} | {bot['pair']} | {bot['direction']}")
            print(f"     Invested: ${bot['invested']:.2f} @ {bot['entry']:.2f}")
            print(f"     Reason: {bot['reason']}")
            print()
        
        if args.all:
            confirm = input(f"\n⚠️  Reset ALL {len(stale)} stale bots? (yes/no): ")
            if confirm.lower() == 'yes':
                for bot in stale:
                    reset_bot(bot['id'], reason=bot['reason'])
                print(f"\n✅ Reset {len(stale)} bots.")
            else:
                print("Cancelled.")
        else:
            print("Use --bot-id <ID> to reset a specific bot, or --all to reset all.")
    
    elif args.bot_id:
        reset_bot(args.bot_id, reason="Manual force reset via CLI")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
