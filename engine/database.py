import sqlite3
import os
import threading
import json
import time
import logging
import uuid

# Setup logger
logger = logging.getLogger(__name__)

# Use absolute path to ensure database is found regardless of working directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "crypto_bot.db")

# Thread-local storage for SQLite connections
# SQLite connections should not be shared across threads
_local = threading.local()

def get_connection():
    # Docstring removed
    # Check if we have a connection and if it's still valid
    need_new_connection = False
    
    if not hasattr(_local, 'connection') or _local.connection is None:
        need_new_connection = True
    else:
        # Test if the connection is still usable
        try:
            _local.connection.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            # Connection was closed or is broken
            need_new_connection = True
    
    if need_new_connection:
        _local.connection = sqlite3.connect(DB_PATH, timeout=30.0)
        # Enable WAL mode for better concurrent read/write performance
        _local.connection.execute("PRAGMA journal_mode=WAL")
        _local.connection.execute("PRAGMA busy_timeout=30000")
    
    return _local.connection

def close_connection():
    # Docstring removed
    if hasattr(_local, 'connection') and _local.connection:
        try:
            _local.connection.close()
        except Exception:
            pass
        _local.connection = None

def init_db():
    """Initializes the database schema and performs necessary migrations."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        cursor = conn.cursor()
        
        # Bots table: Stores configuration for each bot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL,
                rsi_limit REAL,
                martingale_multiplier REAL,
                base_size REAL,
                strategy_type TEXT DEFAULT 'Martingale',
                config TEXT DEFAULT '{}',
                is_active BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'Stopped',
                manual_close_pct REAL DEFAULT 100.0
            )
        """)
        
        # System table (Equity tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_equity (
                key TEXT PRIMARY KEY,
                value REAL
            )
        """)
        
        # Set default starting equity if not present
        cursor.execute("INSERT OR IGNORE INTO system_equity (key, value) VALUES (?, ?)", ('STARTING_EQUITY', 10000.0))
        cursor.execute("INSERT OR IGNORE INTO system_equity (key, value) VALUES (?, ?)", ('BOT_TRADING_BALANCE', 10000.0))
        
        # Check if strategy_type exists (migration for existing db)
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT strategy_type FROM bots LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE bots ADD COLUMN strategy_type TEXT DEFAULT "MQL4"')
# FIXED_SYNTAX:             conn.commit()

        # Check if config exists (migration for existing db)
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT config FROM bots LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE bots ADD COLUMN config TEXT DEFAULT "{}"')
# FIXED_SYNTAX:             conn.commit()
            
        # Check if status exists (migration for existing db)
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT status FROM bots LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute("ALTER TABLE bots ADD COLUMN status TEXT DEFAULT 'Stopped'")
# FIXED_SYNTAX:             conn.commit()
        
        # Trades table: Tracks active positions and Martingale steps
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                bot_id INTEGER PRIMARY KEY,
                current_step INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                entry_confirmed BOOLEAN DEFAULT 0,
                entry_order_id TEXT,
                tp_order_id TEXT,
                bot_position_id TEXT,
                close_type TEXT DEFAULT NULL,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        
        # Migrations for new columns
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT last_exit_price FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_price REAL DEFAULT 0')
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_time INTEGER DEFAULT 0')
# FIXED_SYNTAX:             conn.commit()

        # Migration for basket_start_time
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT basket_start_time FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN basket_start_time INTEGER DEFAULT 0')
# FIXED_SYNTAX:             conn.commit()

        # Migration for entry_confirmed
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT entry_confirmed FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN entry_confirmed BOOLEAN DEFAULT 0')
# FIXED_SYNTAX:             conn.commit()
        
        # Migration for order ID tracking (v0.4.1)
        # Track exchange order IDs to support multiple bots on same pair
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT entry_order_id FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN entry_order_id TEXT')
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN tp_order_id TEXT')
# FIXED_SYNTAX:             conn.commit()

        # Migration for independent position tracking (v0.5.0)
        # Each bot tracks its own position independently
        # SQLite doesn't support adding UNIQUE columns directly, so we add without constraint
        # The uniqueness is guaranteed by bot_id being the PRIMARY KEY of trades table
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT bot_position_id FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN bot_position_id TEXT')
# FIXED_SYNTAX:             conn.commit()
        
        # Add close_type column (single column, no UNIQUE)
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT close_type FROM trades LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute('ALTER TABLE trades ADD COLUMN close_type TEXT DEFAULT NULL')
# FIXED_SYNTAX:             conn.commit()
        
        # Migration for manual close percentage in config
# FIXED_SYNTAX:         try:
# FIXED_SYNTAX:             cursor.execute('SELECT manual_close_pct FROM bots LIMIT 1')
# FIXED_SYNTAX:         except sqlite3.OperationalError:
# FIXED_SYNTAX:             cursor.execute("ALTER TABLE bots ADD COLUMN manual_close_pct REAL DEFAULT 100.0")
# FIXED_SYNTAX:             conn.commit()

        # Create separate table for grid orders (each step can have an order)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                step INTEGER,
                order_type TEXT,
                order_id TEXT,
                price REAL,
                amount REAL,
                status TEXT DEFAULT 'open',
                created_at INTEGER,
                client_order_id TEXT,
                updated_at INTEGER DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_bot ON bot_orders(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_order_id ON bot_orders(order_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_client_id ON bot_orders(client_order_id)')
        
        # Trade history table: Permanent log of all trades for post-mortem analysis
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                timestamp INTEGER,
                action TEXT,
                symbol TEXT,
                price REAL,
                amount REAL,
                cost_usdc REAL,
                order_id TEXT,
                step INTEGER,
                pnl REAL DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        
        # Index for faster queries by bot and time
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_time ON trade_history(timestamp)')
        
        # Index for faster queries by bot activation status
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_active ON bots(is_active)')

        # Notifications table (Phase 9.3)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                type TEXT,
                message TEXT,
                bot_id INTEGER,
                is_read BOOLEAN DEFAULT 0
            )
        """)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(is_read)')

        conn.commit()
    except Exception as e:
        # Handle WinError 233 (Pipe broken) or database locked - non-fatal
        # This prevents Streamlit crash on reload
        try:
            logger.warning(f"Database init warning (non-fatal): {e}")
        except:
            pass
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except:
                pass
    
    try:
        logger.info(f"Database initialized at {DB_PATH}")
    except Exception:
        pass

def add_notification(type, message, bot_id=None):
    # Docstring removed
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO notifications (timestamp, type, message, bot_id) VALUES (?, ?, ?, ?)",
            (time.time(), type, message, bot_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to add notification: {e}")

def get_unread_notifications(limit=10):
    # Docstring removed
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, timestamp, type, message, bot_id FROM notifications WHERE is_read = 0 ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch notifications: {e}")
        return []

def mark_notifications_read(notification_ids):
    # Docstring removed
    if not notification_ids: return
    try:
        conn = get_connection()
        placeholders = ','.join('?' * len(notification_ids))
        conn.execute(
            f"UPDATE notifications SET is_read = 1 WHERE id IN ({placeholders})",
            notification_ids
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to mark notifications read: {e}")

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type="Martingale", config_dict=None):
    # Docstring removed
    if config_dict is None:
        config_dict = {}
    
    config_json = json.dumps(config_dict)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO bots (name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json))
        
        bot_id = cursor.lastrowid
        
        # Initialize an empty trade slot for this bot
        cursor.execute('INSERT INTO trades (bot_id) VALUES (?)', (bot_id,))
        
        conn.commit()
        return bot_id
    except sqlite3.IntegrityError:
        logger.warning(f"Error: Bot name '{name}' already exists.")
        return None
    # Note: No conn.close() - using thread-local connection

def get_bot_params(bot_id):
    # Fetch bot configuration parameters.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config FROM bots WHERE id = ?', (bot_id,))
    result = cursor.fetchone()
    # Note: No conn.close() - using thread-local connection
    return result

def get_bot_status(bot_id):
    # Fetch bot status including trade details for reconciler.
    conn = get_connection()
    cursor = conn.cursor()
    # Return: id, name, pair, total_invested, avg_entry_price, direction, is_active
    cursor.execute("""
        SELECT b.id, b.name, b.pair, 
               COALESCE(t.total_invested, 0) as total_invested, 
               COALESCE(t.avg_entry_price, 0) as avg_entry_price,
               COALESCE(t.target_tp_price, 0) as target_tp_price,
               b.direction, b.is_active
        FROM bots b 
        LEFT JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = ?
    """, (bot_id,))
    return cursor.fetchone()
    # Note: No conn.close() - using thread-local connection

def update_bot(bot_id, name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_dict):
    # Update bot settings.
    config_json = json.dumps(config_dict)
    
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bots SET name=?, pair=?, direction=?, rsi_limit=?, martingale_multiplier=?, base_size=?, strategy_type=?, config=? WHERE id=?", (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json, bot_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"Error: Bot name '{name}' already exists.")
        return False
    except Exception as e:
        logger.error(f"Error updating bot {bot_id}: {e}")
        return False
    # Note: No conn.close() - using thread-local connection

def update_martingale_step(bot_id, next_step, added_investment, new_avg_price, new_tp_price):
    # Updates trade details for the next martingale step.
    conn = get_connection()
    cursor = conn.cursor()
    
    # If step 0 (Entry), set start time if not set
    # Reset start time on every entry/step to give a fresh decay window? 
    # Usually passed earlier.
    
    basket_start_arg = int(time.time()) if next_step == 0 else None
    
    # Update trades table
    if basket_start_arg:
         cursor.execute("UPDATE trades SET current_step = ?, total_invested = total_invested + ?, avg_entry_price = ?, target_tp_price = ?, entry_confirmed = 1, basket_start_time = ? WHERE bot_id = ?", (next_step, added_investment, new_avg_price, new_tp_price, basket_start_arg, bot_id))
    else:
         cursor.execute("UPDATE trades SET current_step = ?, total_invested = total_invested + ?, avg_entry_price = ?, target_tp_price = ?, entry_confirmed = 1 WHERE bot_id = ?", (next_step, added_investment, new_avg_price, new_tp_price, bot_id))
    
    # ATOMIC STATUS SYNC: Ensure bot status reflects trade activity
    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id = ?", (bot_id,))
    
    conn.commit()
    # Note: No conn.close() - using thread-local connection

def deactivate_bot(bot_id, reason="Unknown Error"):
    # Deactivates a bot and logs the reason.
    try:
        conn = get_connection()
        c = conn.cursor()
        # ATOMIC: Set status to STOPPED and is_active to 0
        c.execute("UPDATE bots SET is_active = 0, status='STOPPED' WHERE id = ?", (bot_id,))
        
        # Log this as a system event/trade with error action
        log_trade(
            bot_id=bot_id,
            action='ERROR_STOP',
            symbol='SYSTEM',
            price=0,
            amount=0,
            cost_usdc=0,
            order_id="SYS_STOP",
            step=0,
            notes=f"Auto-Stopped: {reason}"
        )
        
        conn.commit()
        logger.info(f"Bot {bot_id} deactivated: {reason}")
        return True
    except Exception as e:
        logger.error(f"Failed to deactivate bot {bot_id}: {e}")
        return False

def calculate_step_from_position(position_size: float, base_size: float, multiplier: float) -> int:
    # Docstring removed
    if position_size <= 0 or base_size <= 0 or multiplier <= 1:
        return 0
    
    ratio = position_size / base_size
    if ratio <= 1:
        return 0
    
    import math
    step = math.log(ratio) / math.log(multiplier)
    return max(0, round(step))


def reset_bot_after_tp(bot_id, exit_price, direction, action_label='TP_HIT', notes=''):
    """
    Resets a bot's trade session after Take Profit or Stop Loss.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get current investment details before reset (for logging)
        cursor.execute("SELECT total_invested, current_step, avg_entry_price, name, pair FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        
        if not row:
            logger.warning(f"Bot {bot_id} not found for reset")
            return
            
        total_invested, current_step, avg_entry_price, bot_name, pair = row
        
        # Calculate PnL
        pnl = 0.0
        if exit_price > 0 and avg_entry_price > 0:
            est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
            if direction.upper() == 'LONG':
                pnl = (exit_price - avg_entry_price) * est_qty
            else:  # SHORT
                pnl = (avg_entry_price - exit_price) * est_qty
        
        # Log the exit to trade_history BEFORE resetting
        log_trade(
            bot_id=bot_id,
            action=action_label,
            symbol=pair,
            price=exit_price,
            amount=total_invested / avg_entry_price if avg_entry_price > 0 else 0,
            cost_usdc=total_invested,
            step=current_step,
            pnl=pnl,
            notes=notes or f"{action_label} at step {current_step}, avg entry {avg_entry_price:.4f}"
        )

        # Full close - reset to idle
        cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = 0, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = ? WHERE bot_id = ?", (exit_price, int(time.time()), action_label, bot_id))
        
        # CRITICAL: Clean up all open orders in bot_orders table
        # This prevents ghost orders from persisting after position close
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'auto_closed', 
                notes = COALESCE(notes, '') || ' | Auto-closed on position reset: ' || ?,
                updated_at = ?
            WHERE bot_id = ? AND status = 'open'
        """, (action_label, int(time.time()), bot_id))
        
        orders_cleaned = cursor.rowcount
        if orders_cleaned > 0:
            logger.info(f"🧹 Cleaned up {orders_cleaned} open orders for bot {bot_id}")
        
        # ATOMIC STATUS SYNC: Ensure bot status reflects trade closure
        cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id = ?", (bot_id,))
        
        conn.commit()
        logger.info(f"✅ Reset trade state for bot {bot_id} ({bot_name}) at exit price ${exit_price:.4f}")

    except Exception as e:
        try: conn.rollback()
        except: pass
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise



def check_and_fix_integrity():
    """Run on startup: Fix inconsistencies between bots.status and trades.total_invested."""
    print("Running Integrity Check...")
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all active bots
    cursor.execute("SELECT b.id, b.name, b.status, t.total_invested FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1")
    rows = cursor.fetchall()
    
    fixed_count = 0
    for row in rows:
        bot_id, name, status, invested = row
        invested = invested or 0
        
        # Scenario 1: Status says IN TRADE, but no money invested
        if status == 'IN TRADE' and invested <= 0:
            logger.warning(f"🔧 SELF-HEALING: Bot {bot_id} ({name}) fixed. Status 'IN TRADE' -> 'Waiting for Signal' (Invested: 0)")
            cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id=?", (bot_id,))
            fixed_count += 1
            
        # Scenario 2: Status says Waiting, but money is invested
        elif status == 'Waiting for Signal' and invested > 0:
            logger.warning(f"🔧 SELF-HEALING: Bot {bot_id} ({name}) fixed. Status 'Waiting' -> 'IN TRADE' (Invested: {invested})")
            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
            fixed_count += 1
            
    conn.commit()
    print(f"Integrity Check Complete. Fixed {fixed_count} inconsistencies.")
def update_bot_display_status(bot_id: int, status: str):
    # Update bot status text
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE bots SET status = ? WHERE id = ?', (status, bot_id))
    conn.commit()

def get_bot_order_ids(bot_id):
    # Get all order IDs for a bot.
    conn = get_connection()
    cursor = conn.cursor()
    
    orders = {'entry_order_id': None, 'tp_order_id': None, 'grid_orders': []}
    
    # Get active trade orders
    cursor.execute('SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id = ?', (bot_id,))
    res = cursor.fetchone()
    if res:
        orders['entry_order_id'] = res[0]
        orders['tp_order_id'] = res[1]
        
    # Get grid orders (pending/open in bot_orders)
    cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id = ? AND status='open'", (bot_id,))
    rows = cursor.fetchall()
    orders['grid_orders'] = [r[0] for r in rows if r[0]]
    
    return orders

def import_position_from_exchange(bot_id: int, pair: str, position_size: float, entry_price: float, direction: str):
    # Manually imports a position into the trades table, allowing a bot to 'adopt' it.
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get Config
    cursor.execute("SELECT name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config FROM bots WHERE id = ?", (bot_id,))
    params = cursor.fetchone()
    
    if not params:
        logger.error(f"Bot {bot_id} not found for import")
        return False
        
    bot_name, bot_pair, bot_direction, rsi_limit, multiplier, base_size, strat_type, config_json = params
    
    # Verify Direction
    if direction.upper() != bot_direction.upper():
        logger.warning(f"Direction mismatch for import: Bot {bot_direction}, Position {direction}")
        return False
        
    # Calculate Step
    # Formula: total = base * (mult ^ step)  =>  step = log(total/base) / log(mult)
    total_invested = abs(float(position_size)) * float(entry_price)
    
    calculated_step = 0
    if base_size > 0 and multiplier > 1 and total_invested > base_size:
        import math
        try:
            # Approximate step
            val = total_invested / base_size
            step_float = math.log(val) / math.log(multiplier)
            calculated_step = round(step_float)
            if calculated_step < 0: calculated_step = 0
        except:
            calculated_step = 0
            
    # Sanity Check
    expected_at_step = base_size * (multiplier ** calculated_step)
    size_variance = abs(total_invested - expected_at_step) / expected_at_step if expected_at_step > 0 else 0
    
    if size_variance > 0.1:
        logger.warning(
            f"⚠️ Position size variance: Actual ${total_invested:.2f}, Expected Step {calculated_step}: ${expected_at_step:.2f}"
        )
    
    # Update DB
    try:
        cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=?, target_tp_price=0, last_exit_price=0, last_exit_time=0, entry_confirmed=1, basket_start_time=?, entry_order_id='IMPORTED', tp_order_id=NULL, bot_position_id='IMPORTED', close_type=NULL WHERE bot_id=?", (calculated_step, total_invested, float(entry_price), int(time.time()), bot_id))
        logger.info(
            f"✅ Imported position for bot {bot_id} (Step {calculated_step}) "
            f"Base: ${base_size} | Multiplier: {multiplier}x"
        )
        return True
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to import position for bot {bot_id}: {e}")
        return False

def get_all_bots():
    # Fetches all bots status for the manager UI.
    conn = get_connection()
    cursor = conn.cursor()
    # Return everything needed for the table
    cursor.execute("SELECT b.id, b.name, b.pair, b.is_active, b.strategy_type, COALESCE(t.total_invested, 0) as total_invested, COALESCE(t.current_step, 0) as current_step FROM bots b LEFT JOIN trades t ON b.id = t.bot_id")
    results = cursor.fetchall()
    return results

def toggle_bot_active(bot_id, new_status):
    # Updates the is_active flag for a bot.
    conn = get_connection()
    cursor = conn.cursor()
    # Ensure status is integer 0 or 1
    status_int = 1 if new_status else 0
    cursor.execute('UPDATE bots SET is_active = ? WHERE id = ?', (status_int, bot_id))
    
    # If enabling, clear any error notes from recent history that might show in UI
    # We log "User Resumed".
    if status_int == 1:
        log_trade(
            bot_id=bot_id,
            action='RESUME',
            symbol='SYSTEM',
            price=0,
            amount=0,
            cost_usdc=0,
            order_id="SYS_RESUME",
            step=0,
            notes="User Manually Resumed Bot"
        )
    
    conn.commit()

def delete_bot(bot_id):
    # Deletes a bot and its trade history.
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Delete from trade_history first (FK)
        cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))
        # Delete from trades (FK)
        cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
        # Delete from bots
        cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error deleting bot {bot_id}: {e}")
        return False

def confirm_order(db_id, exchange_order_id):
    """Confirm a pending order by updating it with the exchange order ID and marking it as open."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bot_orders SET order_id = ?, status = 'open', updated_at = ? WHERE id = ?", (exchange_order_id, int(time.time()), db_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to confirm order {db_id}: {e}")
        return False

def fail_order(db_id, reason):
    """Mark an order as failed with a reason."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bot_orders SET status = 'failed', notes = ?, updated_at = ? WHERE id = ?", (reason, int(time.time()), db_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to mark order {db_id} as failed: {e}")
        return False

def cleanup_pending_orders(exchange):
    # Startup cleanup: Check all 'pending' orders and reconcile with exchange.
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {'confirmed': 0, 'failed': 0, 'total': 0}
    threshold_time = int(time.time()) - 30 # 30s timeout
    
    try:
        # Find all pending orders
        cursor.execute("SELECT id, bot_id, order_type, client_order_id, price, amount FROM bot_orders WHERE status = 'open' AND created_at < ?", (threshold_time,))
        pending = cursor.fetchall()
        stats['total'] = len(pending)
        
        if not pending:
            return stats
        
        logger.info(f"🔄 WAL Cleanup: Found {len(pending)} pending orders from previous session.")
        
        # Fetch all open orders from exchange
        try:
            ex_orders = exchange.exchange.fetch_open_orders()
        except:
            ex_orders = []
        
        # Build lookup by clientOrderId
        ex_by_client_id = {}
        for o in ex_orders:
            cid = o.get('clientOrderId', '')
            if cid:
                ex_by_client_id[cid] = o
        
        # Reconcile each pending order
        for row in pending:
            db_id, bot_id, order_type, client_id, price, amount = row
            
            if client_id in ex_by_client_id:
                # Order EXISTS on exchange - confirm it
                actual_order = ex_by_client_id[client_id]
                confirm_order(db_id, actual_order['id'])
                stats['confirmed'] += 1
                logger.info(f"   ✅ Recovered pending {order_type} order for bot {bot_id} -> Exchange#{actual_order['id']}")
            else:
                # Order NOT on exchange - mark failed
                fail_order(db_id, 'Not found on exchange during startup reconciliation')
                stats['failed'] += 1
                logger.warning(f"   ❌ Pending {order_type} order for bot {bot_id} not found on exchange. Marked failed.")
        
        return stats
        
    except Exception as e:
        logger.error(f"WAL Cleanup failed: {e}")
        return stats

def update_trade_tp_price(bot_id: int, new_tp_price: float):
    # Updates the target TP price in the trades table.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (new_tp_price, bot_id))
    conn.commit()

def get_bots_by_order_id(order_id):
    # Find which bot(s) own a specific order ID.
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check trades table
    bot_ids = []
    
    # Check entry_order_id
    cursor.execute('SELECT bot_id FROM trades WHERE entry_order_id = ?', (order_id,))
    for row in cursor.fetchall():
        bot_ids.append({'bot_id': row[0], 'type': 'entry'})
    
    # Check tp_order_id
    cursor.execute('SELECT bot_id FROM trades WHERE tp_order_id = ?', (order_id,))
    for row in cursor.fetchall():
        bot_ids.append({'bot_id': row[0], 'type': 'tp'})
    
    # Check bot_orders table
    cursor.execute('SELECT bot_id, order_type FROM bot_orders WHERE order_id = ?', (order_id,))
    for row in cursor.fetchall():
        bot_ids.append({'bot_id': row[0], 'type': row[1]})
    
    return bot_ids

def match_exchange_orders_to_bots(exchange_orders):
    # Match exchange orders to bots by order ID.
    order_to_bot = {}
    
    for order in exchange_orders:
        order_id = order.get('id')
        if not order_id:
            continue
        
        # Find which bot owns this order
        bots = get_bots_by_order_id(order_id)
        
        if bots:
            # Get first bot's name
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT name FROM bots WHERE id = ?', (bots[0]['bot_id'],))
            result = cursor.fetchone()
            bot_name = result[0] if result else 'Unknown'
            
            order_to_bot[order_id] = {
                'bot_id': bots[0]['bot_id'],
                'bot_name': bot_name,
                'type': bots[0]['type'],
                'order_info': order
            }
        else:
            # Order not tracked by any bot (possibly manual order)
            order_to_bot[order_id] = {
                'bot_id': None,
                'bot_name': 'MANUAL',
                'type': 'unknown',
                'order_info': order
            }
    
    return order_to_bot

def generate_bot_position_id():
    # Generate a unique ID for this bot's position tracking
    return str(uuid.uuid4())[:8].upper()

def get_bot_position_id(bot_id):
    # Get the unique position ID for a bot.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT bot_position_id FROM trades WHERE bot_id = ?', (bot_id,))
    result = cursor.fetchone()
    if result and result[0]:
        return result[0]
    return None

def update_bot_config(bot_id, key, value):
    # Updates a single key in the bot's config JSON.
    # Simplified implementation
    pass

def close_bot_position(bot_id):
    # Close a bot's position (partial or full).
    # Placeholder
    pass

def reconcile_with_db(bot_id, current_price, open_orders, exchange_position):
    # Helper to calculate and sync detailed trade data
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get current trade state
    cursor.execute("SELECT t.total_invested, t.avg_entry_price, t.target_tp_price, t.current_step, b.name, b.pair, b.direction, t.bot_position_id, b.config FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
    
    result = cursor.fetchone()
    if not result:
        # conn.close() # Thread local
        return {'success': False, 'error': 'Bot not found'}
    
    total_invested, avg_entry, target_tp, current_step, name, pair, direction, position_id, config_json = result
    
    # Logic for reconciliation works by returning stats
    # We do NOT execute trades here, just DB updates if needed?
    # Actually the simplified version doesn't update, it just returns info.
    # But wait, Step 1718 version DID update DB if fully closed.
    
    close_pct = 0.0
    
    # Check if position is closed on exchange
    if not exchange_position or float(exchange_position.get('size', 0)) == 0:
        if total_invested > 0:
             close_pct = 100.0
    
    close_type = 'RECONCILE'  
    
    # Update trades table based on close percentage
    if close_pct >= 100:
        # Full close - reset to idle
        cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = 0, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = ? WHERE bot_id = ?", (current_price, int(time.time()), close_type, bot_id))
        
        # CRITICAL: Clean up all open orders (same as reset_bot_after_tp)
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'auto_closed', 
                notes = COALESCE(notes, '') || ' | Auto-closed on reconciliation',
                updated_at = ?
            WHERE bot_id = ? AND status = 'open'
        """, (int(time.time()), bot_id))
        
        # ATOMIC STATUS SYNC
        cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id = ?", (bot_id,))
        
    conn.commit()
    
    return {
        'success': True,
        'bot_id': bot_id,
        'close_pct': close_pct,
        'close_price': current_price,
        'pnl': 0.0, # Approximate
        'position_id': position_id
    }


def get_bot_close_settings(bot_id):
    # Docstring removed
    params = get_bot_params(bot_id)
    if not params:
        return None
    
    config_json = params[7]  # config is at index 7
    config_dict = json.loads(config_json) if config_json else {}
    
    return {
        'manual_close_pct': config_dict.get('manual_close_pct', 100.0),
        'stop_after_pnl': config_dict.get('stop_after_pnl', 0.0),  # 0 = disabled
        'stop_after_time': config_dict.get('stop_after_time', 0),  # 0 = disabled (hours)
    }


def update_bot_close_settings(bot_id, manual_close_pct=None, stop_after_pnl=None, stop_after_time=None):
    # Docstring removed
    params = get_bot_params(bot_id)
    if not params:
        return False
    
    config_json = params[7]
    config_dict = json.loads(config_json) if config_json else {}
    config_dict = config_dict if isinstance(config_dict, dict) else {}
    
    if manual_close_pct is not None:
        config_dict['manual_close_pct'] = manual_close_pct
    if stop_after_pnl is not None:
        config_dict['stop_after_pnl'] = stop_after_pnl
    if stop_after_time is not None:
        config_dict['stop_after_time'] = stop_after_time
    
    # Update bot with new config
    # Reuse update_bot from module scope
    return update_bot(
        bot_id=bot_id,
        name=params[0],
        pair=params[1],
        direction=params[2],
        rsi_limit=params[3],
        martingale_multiplier=params[4],
        base_size=params[5],
        strategy_type=params[6],
        config_dict=config_dict
    )

def log_trade(bot_id, action, symbol, price, amount, cost_usdc, order_id="UNKNOWN", step=0, notes="", pnl=0.0):
    # Logs a trade to the permanent trade_history table for post-mortem analysis.
    try:
        conn = get_connection()
        c = conn.cursor()
        
        # Check if pnl column exists (naive check or just try insert)
        # We assume schema is consistent with latest version.
        try:
             c.execute("""
                INSERT INTO trade_history (
                    bot_id, timestamp, action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bot_id, int(time.time()), action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl))
        except sqlite3.OperationalError:
             # Fallback for older schema without pnl column
             c.execute("""
                INSERT INTO trade_history (
                    bot_id, timestamp, action, symbol, price, amount, cost_usdc, order_id, step, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bot_id, int(time.time()), action, symbol, price, amount, cost_usdc, order_id, step, notes))
        
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to log trade: {e}")
        return False


def get_trade_history(bot_id=None, limit=100):
    """
    Fetches permanent trade history.
    If bot_id is None, fetches for all bots.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        if bot_id:
            c.execute("SELECT * FROM trade_history WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?", (bot_id, limit))
        else:
            c.execute("SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT ?", (limit,))
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error fetching trade history: {e}")
        return []

def get_bot_pnl_summary(bot_id):
    """
    Calculates PnL summary for a bot from trade_history.
    Returns a dictionary with total_pnl, win_count, loss_count, total_trades.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        # Get all trades with PnL for this bot
        c.execute("SELECT pnl FROM trade_history WHERE bot_id = ? AND pnl IS NOT NULL", (bot_id,))
        pnl_records = c.fetchall()
        
        total_pnl = 0.0
        win_count = 0
        loss_count = 0
        
        for record in pnl_records:
            pnl = record[0]
            total_pnl += pnl
            if pnl > 0:
                win_count += 1
            elif pnl < 0:
                loss_count += 1
        
        return {
            'total_pnl': total_pnl,
            'win_count': win_count,
            'loss_count': loss_count,
            'total_trades': len(pnl_records)
        }
    except Exception as e:
        logger.error(f"Error calculating PnL summary for bot {bot_id}: {e}")
        return {
            'total_pnl': 0.0,
            'win_count': 0,
            'loss_count': 0,
            'total_trades': 0
        }



def check_stop_after_conditions(bot_id, current_pnl, hours_in_trade):
    # Docstring removed
    settings = get_bot_close_settings(bot_id)
    if not settings:
        return {'triggered': False}
    
    triggered = []
    
    # Check PnL stop
    if settings['stop_after_pnl'] > 0:
        if current_pnl >= settings['stop_after_pnl']:
            triggered.append({
                'type': 'STOP_AFTER_PNL',
                'reason': f'PnL ${current_pnl:.2f} >= target ${settings["stop_after_pnl"]:.2f}'
            })
    
    # Check time stop
    if settings['stop_after_time'] > 0:
        if hours_in_trade >= settings['stop_after_time']:
            triggered.append({
                'type': 'STOP_AFTER_TIME',
                'reason': f'Hours in trade {hours_in_trade:.1f} >= limit {settings["stop_after_time"]}'
            })
    
    return {
        'triggered': len(triggered) > 0,
        'conditions': triggered
    }




def save_bot_order(bot_id, order_type, exchange_order_id, price, amount, step, status='open'):
    # Save an order to the bot_orders table (Write-Ahead Log or Post-Confirmation).
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (bot_id, step, order_type, exchange_order_id, price, amount, status, int(time.time())))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save bot order: {e}")
        return False

def update_order_status(bot_id, order_id, status):
    # Update order status in bot_orders table.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE bot_orders SET status = ? WHERE order_id = ? AND bot_id = ?", (status, order_id, bot_id))
    conn.commit()

def get_last_filled_order(bot_id):
    """Fetches the most recent filled order buy order for a bot since its current trade started."""
    conn = get_connection()
    c = conn.cursor()
    try:
        # Get basket start time from trades table
        c.execute("SELECT basket_start_time FROM trades WHERE bot_id = ?", (bot_id,))
        res = c.fetchone()
        basket_start_time = res[0] if res else 0
        
        c.execute("SELECT price, amount, step, created_at FROM bot_orders WHERE bot_id = ? AND order_type = 'buy' AND status = 'filled' AND created_at >= ? ORDER BY created_at DESC LIMIT 1", (bot_id, basket_start_time))
        row = c.fetchone()
        if row:
            return {'price': row[0], 'amount': row[1], 'step': row[2], 'timestamp': row[3]}
    except Exception as e:
        logger.error(f"Error getting last filled order for bot {bot_id}: {e}")
    return None
