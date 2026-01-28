
import sys
import logging
import sqlite3
import time
from engine.bot_executor import BotExecutor
from engine.exchange_interface import ExchangeInterface
from engine.database import add_bot, get_bot_status, reset_bot_after_tp

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("TestSelfHealing")

class MockRunner:
    def __init__(self):
        self.exchange = None
        self.strategies = {}
        # Mock attributes needed by check_order_limits
        self._last_reset_day = time.strftime("%Y-%m-%d")
        self.orders_this_cycle = 0
        self.orders_today = {}

def test_healing():
    pair = 'BTC/USDT'
    
    # 1. Check if Real Position exists (Safety First)
    logger.info("Initializing Exchange...")
    ex = ExchangeInterface(market_type='future')
    
    positions = ex.fetch_positions()
    target_clean = pair.replace('/', '').split(':')[0]
    has_real_pos = False
    for p in positions:
        if not p: continue
        p_sym = p.get('symbol', '').replace('/', '').split(':')[0]
        if p_sym == target_clean:
            size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if size != 0:
                has_real_pos = True
                logger.warning(f"⚠️ Real Position detected on {pair} (Size: {size}). Test cannot run safely.")
                return

    if has_real_pos: return

    # 2. Create Ghost Bot
    logger.info("Creating Ghost Bot in DB...")
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    # Bot
    cur.execute("INSERT INTO bots (name, pair, direction, is_active, config, rsi_limit, martingale_multiplier, base_size) VALUES (?, ?, ?, ?, ?, 30, 1.5, 10.0)",
                ('GhostBot_Tester', pair, 'LONG', 1, '{}'))
    bot_id = cur.lastrowid
    
    # Fake Trade (Ghost)
    cur.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time) VALUES (?, ?, ?, ?, ?, ?)",
                (bot_id, 0, 50.0, 50000.0, 1, int(time.time())))
    conn.commit()
    conn.close()
    
    try:
        # Verify it looks active
        status = get_bot_status(bot_id)
        if not status or status[3] <= 0: # invested
            logger.error("DB Setup Failed. Bot does not appear 'In Trade'.")
            return
        logger.info(f"Ghost Bot {bot_id} Created. Invested: ${status[3]}")

        # 3. Run Verification
        logger.info("Running verify_state_sync (Expect Auto-Heal)...")
        runner = MockRunner()
        executor = BotExecutor(runner)
        
        # This should return FALSE (Invalid State) and reset DB
        is_valid = executor.verify_state_sync(bot_id, 'GhostBot_Tester', pair, ex)
        
        logger.info(f"Sync Result: {is_valid}")
        
        if is_valid is False:
            logger.info("✅ SUCCESS: verify_state_sync detected Ghost Trade and returned False.")
        else:
            logger.error("❌ FAILURE: verify_state_sync returned True (failed to detect Ghost Trade).")

        # 4. Verify DB Reset
        status_after = get_bot_status(bot_id)
        # Check if invested is now 0 (or None, or small if floating point, but should be 0)
        invested = status_after[3] if status_after else 0
        if invested > 0:
             logger.error(f"❌ DB Check Failed: Bot still has invested funds (${invested}).")
        else:
             logger.info("✅ DB Check Passed: Bot state is clean (Invested = 0).")

    finally:
        # Cleanup
        logger.info("Cleaning up test bot...")
        conn = sqlite3.connect('crypto_bot.db')
        conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
        conn.commit()
        conn.close()

if __name__ == "__main__":
    test_healing()
