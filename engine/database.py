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
    """
    Returns a thread-safe connection to the SQLite database.
    Each thread gets its own connection to prevent 'database is locked' errors.
    """
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
    """Explicitly closes the thread-local connection."""
    if hasattr(_local, 'connection') and _local.connection:
        try:
            _local.connection.close()
        except Exception:
            pass
        _local.connection = None

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    # Use a fresh connection for init (not thread-local) since this runs once at startup
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        cursor = conn.cursor()
        
        # Bots table: Stores configuration for each bot
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                strategy_type TEXT DEFAULT 'MQL4',
                pair TEXT NOT NULL,
                direction TEXT CHECK(direction IN ('LONG', 'SHORT')) NOT NULL,
                rsi_limit REAL NOT NULL,
                martingale_multiplier REAL NOT NULL,
                base_size REAL NOT NULL,
                config TEXT DEFAULT '{}',
                is_active BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'Stopped'
            )
        ''')
        
        # Bots table: Stores configuration for each bot
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                strategy_type TEXT DEFAULT 'MQL4',
                pair TEXT NOT NULL,
                direction TEXT CHECK(direction IN ('LONG', 'SHORT')) NOT NULL,
                rsi_limit REAL NOT NULL,
                martingale_multiplier REAL NOT NULL,
                base_size REAL NOT NULL,
                config TEXT DEFAULT '{}',
                is_active BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'Stopped'
            )
        ''')
        
        # System table: Stores global, non-bot-specific configurations/state
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_equity (
                key TEXT PRIMARY KEY,
                value REAL
            )
        ''')
        
        # Set default starting equity if not present
        cursor.execute("INSERT OR IGNORE INTO system_equity (key, value) VALUES (?, ?)", ('STARTING_EQUITY', 10000.0))
        cursor.execute("INSERT OR IGNORE INTO system_equity (key, value) VALUES (?, ?)", ('BOT_TRADING_BALANCE', 10000.0))
        
        # Check if strategy_type exists (migration for existing db)
        try:
            cursor.execute('SELECT strategy_type FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE bots ADD COLUMN strategy_type TEXT DEFAULT "MQL4"')
            conn.commit()

        # Check if config exists (migration for existing db)
        try:
            cursor.execute('SELECT config FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE bots ADD COLUMN config TEXT DEFAULT "{}"')
            conn.commit()
            
        # Check if status exists (migration for existing db)
        try:
            cursor.execute('SELECT status FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bots ADD COLUMN status TEXT DEFAULT 'Stopped'")
            conn.commit()
        
        # Trades table: Tracks active positions and Martingale steps
        # bot_id is a foreign key linked to bots.id
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                bot_id INTEGER PRIMARY KEY,
                current_step INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        ''')
        
        # Migrations for new columns
        try:
            cursor.execute('SELECT last_exit_price FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_price REAL DEFAULT 0')
            cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_time INTEGER DEFAULT 0')
            conn.commit()

        # Migration for basket_start_time
        try:
            cursor.execute('SELECT basket_start_time FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN basket_start_time INTEGER DEFAULT 0')
            conn.commit()

        # Migration for entry_confirmed
        try:
            cursor.execute('SELECT entry_confirmed FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN entry_confirmed BOOLEAN DEFAULT 0')
            conn.commit()
        
        # Migration for order ID tracking (v0.4.1)
        # Track exchange order IDs to support multiple bots on same pair
        try:
            cursor.execute('SELECT entry_order_id FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN entry_order_id TEXT')
            cursor.execute('ALTER TABLE trades ADD COLUMN tp_order_id TEXT')
            conn.commit()

        # Migration for independent position tracking (v0.5.0)
        # Each bot tracks its own position independently
        # SQLite doesn't support adding UNIQUE columns directly, so we add without constraint
        # The uniqueness is guaranteed by bot_id being the PRIMARY KEY of trades table
        try:
            cursor.execute('SELECT bot_position_id FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN bot_position_id TEXT')
            conn.commit()
        
        # Add close_type column (single column, no UNIQUE)
        try:
            cursor.execute('SELECT close_type FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN close_type TEXT DEFAULT NULL')
            conn.commit()
        
        # Migration for manual close percentage in config
        try:
            cursor.execute('SELECT manual_close_pct FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bots ADD COLUMN manual_close_pct REAL DEFAULT 100.0")
            conn.commit()

        # Create separate table for grid orders (each step can have an order)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                step INTEGER NOT NULL,
                order_type TEXT NOT NULL,  -- 'entry', 'tp', 'grid'
                order_id TEXT,  -- Exchange order ID
                price REAL,
                amount REAL,
                status TEXT DEFAULT 'open',  -- 'open', 'filled', 'cancelled'
                created_at INTEGER DEFAULT 0,
                filled_at INTEGER DEFAULT 0,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_bot ON bot_orders(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_order_id ON bot_orders(order_id)')
        
        # Trade history table: Permanent log of all trades for post-mortem analysis
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                cost_usdc REAL DEFAULT 0,
                order_id TEXT,
                step INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0,
                timestamp INTEGER NOT NULL,
                notes TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        ''')
        
        # Index for faster queries by bot and time
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_time ON trade_history(timestamp)')
        
        # Index for faster queries by bot activation status
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_active ON bots(is_active)')

        conn.commit()
    except Exception as e:
        # Handle WinError 233 (Pipe broken) or database locked - non-fatal
        # This prevents Streamlit crash on reload
        try:
            logger.warning(f"Database init warning (non-fatal): {e}")
        except:
            pass  # Logger itself might be broken
        return  # Exit gracefully
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

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type="Martingale", config_dict=None):
    """Adds a new bot and initializes its trade state."""
    if config_dict is None:
        config_dict = {}
    
    config_json = json.dumps(config_dict)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO bots (name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json))
        
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
    """Fetches all configuration parameters for a specific bot."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config FROM bots WHERE id = ?', (bot_id,))
    result = cursor.fetchone()
    # Note: No conn.close() - using thread-local connection
    return result

def update_bot(bot_id, name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_dict):
    """Updates an existing bot configuration."""
    config_json = json.dumps(config_dict)
    
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE bots 
            SET name = ?, pair = ?, direction = ?, rsi_limit = ?, 
                martingale_multiplier = ?, base_size = ?, 
                strategy_type = ?, config = ?
            WHERE id = ?
        ''', (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json, bot_id))
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
    """Updates the active position stats when a new Martingale step is triggered."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # If step 0 (Entry), set start time if not set
    current_time = int(time.time())
    
    # Reset start time on every entry/step to give a fresh decay window
    cursor.execute('''
        UPDATE trades
        SET current_step = ?,
            total_invested = total_invested + ?,
            avg_entry_price = ?,
            target_tp_price = ?,
            entry_confirmed = 1,
            basket_start_time = ?
        WHERE bot_id = ?
    ''', (next_step, added_investment, new_avg_price, new_tp_price, current_time, bot_id))
    
    conn.commit()
    # Note: No conn.close() - using thread-local connection

def deactivate_bot(bot_id, reason="Unknown Error"):
    """Deactivates a bot and logs the reason."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('UPDATE bots SET is_active = 0 WHERE id = ?', (bot_id,))
        
        # Log this as a system event/trade with error action
        # Using log_trade to ensure it appears in history
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
        # Note: No conn.close() - using thread-local connection
        logger.info(f"Bot {bot_id} deactivated: {reason}")
        return True
    except Exception as e:
        logger.error(f"Failed to deactivate bot {bot_id}: {e}")
        return False

def calculate_step_from_position(position_size: float, base_size: float, multiplier: float) -> int:
    """
    Calculate the martingale step from a position size.
    
    Uses the formula: position_size = base_size * (multiplier ^ step)
    Solving for step: step = log(position_size / base_size) / log(multiplier)
    
    Args:
        position_size: The position size in USD
        base_size: The bot's base entry size
        multiplier: The martingale multiplier
        
    Returns:
        The calculated step (rounded to nearest integer), minimum 0
    """
    if position_size <= 0 or base_size <= 0 or multiplier <= 1:
        return 0
    
    ratio = position_size / base_size
    if ratio <= 1:
        return 0
    
    import math
    step = math.log(ratio) / math.log(multiplier)
    return max(0, round(step))


def reset_bot_after_tp(bot_id, exit_price=0.0, action_label='TP_HIT', exchange_positions=None, verify_with_exchange=True):
    """
    Called when TP is hit or when clearing a position.
    
    PROFESSIONAL STATE MACHINE LOGIC:
    1. Calculate PnL if possible.
    2. Log to trade_history.
    3. Only reset trade state IF:
       - verify_with_exchange=False (force reset), OR
       - Exchange has no position for this bot's pair (verified position is closed)
    
    This prevents premature clearing when:
    - Engine restarts and calls reset on stop
    - Reconciliation has timing issues
    - Exchange API temporarily returns empty positions
    
    Args:
        bot_id: The bot to reset
        exit_price: Price at which position was closed
        action_label: Action name for trade history (TP_HIT, OFFLINE_CLOSE, etc.)
        exchange_positions: Optional dict of {pair: position_data} to avoid extra API calls
        verify_with_exchange: If True, checks exchange before clearing
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get current trade details and bot config
    cursor.execute('''
        SELECT total_invested, avg_entry_price, current_step, b.name, b.pair, b.direction, b.base_size, b.martingale_multiplier
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE t.bot_id = ?
    ''', (bot_id,))
    
    row = cursor.fetchone()
    if not row:
        logger.warning(f"Bot {bot_id} not found for reset")
        return
        
    total_invested, avg_entry_price, current_step, bot_name, pair, direction, base_size, mult = row
    
    # Check if there's actually a position to reset
    if total_invested <= 0:
        logger.info(f"Bot {bot_id} ({bot_name}) already has no position, skipping reset")
        return
    
    try:
        # VERIFICATION LOGIC: Only reset if position is actually closed on exchange
        if verify_with_exchange and exchange_positions is not None:
            # Normalize pair for exchange lookup
            exchange_pair = pair.split(':')[0] if ':' in pair else pair
            
            # Check if exchange has a position for this pair
            ex_pos = exchange_positions.get(exchange_pair) or exchange_positions.get(pair)
            
            if ex_pos and abs(ex_pos.get('size', 0)) > 0:
                # Exchange still has position! Do NOT reset!
                logger.critical(
                    f"🚨 SAFETY BLOCK: Bot {bot_id} ({bot_name}) has ${total_invested:.2f} invested "
                    f"but exchange shows {ex_pos.get('size', 0):.6f} {exchange_pair} position! "
                    f"REFUSING to reset. Manual investigation required."
                )
                # Log this safety block
                log_trade(
                    bot_id=bot_id,
                    action='RESET_BLOCKED',
                    symbol=pair,
                    price=exit_price,
                    amount=total_invested / avg_entry_price if avg_entry_price > 0 else 0,
                    cost_usdc=total_invested,
                    step=current_step,
                    pnl=0,
                    notes=f"RESET BLOCKED: Exchange still has position, refusing to clear DB state"
                )
                return  # DO NOT PROCEED WITH RESET
        
        # Calculate PnL
        pnl = 0.0
        if exit_price > 0 and avg_entry_price > 0:
            est_qty = total_invested / avg_entry_price
            if direction.upper() == 'LONG':
                pnl = (exit_price - avg_entry_price) * est_qty
            else:  # SHORT
                pnl = (avg_entry_price - exit_price) * est_qty
            logger.info(f"{action_label} PnL for {bot_name}: Exit=${exit_price:.4f}, Entry=${avg_entry_price:.4f}, PnL=${pnl:.2f}")

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
            notes=f"{action_label} at step {current_step}, avg entry {avg_entry_price:.4f}"
        )
        logger.info(f"Logged {action_label} to trade_history for {bot_name}")

        # Reset the trade state
        cursor.execute('''
            UPDATE trades
            SET current_step = 0,
            total_invested = 0,
            avg_entry_price = 0,
            target_tp_price = 0,
            last_exit_price = ?,
            last_exit_time = ?,
            basket_start_time = 0,
            entry_confirmed = 0,
            entry_order_id = NULL,
            tp_order_id = NULL,
            bot_position_id = NULL
        WHERE bot_id = ?
        ''', (exit_price, int(time.time()), bot_id))
        conn.commit()
        logger.info(f"✅ Reset trade state for bot {bot_id} ({bot_name}) at exit price ${exit_price:.4f}")

    except Exception as e:
        # Rollback on any error
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise

def get_bot_status(bot_id):
    """Fetches full status joining bot settings and trade data."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, t.last_exit_price, t.last_exit_time, t.basket_start_time
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = ?
    ''', (bot_id,))
    result = cursor.fetchone()
    # Note: No conn.close() - using thread-local connection
    return result

def import_position_from_exchange(bot_id: int, pair: str, position_size: float, entry_price: float, direction: str):
    """
    Import a position from exchange into the database.
    Used during reconciliation when orphaned positions are detected.
    
    PROFESSIONAL LOGIC:
    1. Fetch bot's base_size and multiplier to calculate correct martingale step
    2. Calculate step from position size: step = log(position/base) / log(multiplier)
    3. Set all position fields correctly
    4. Log the import with correct step for audit trail
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get bot's configuration for step calculation
        cursor.execute('SELECT base_size, martingale_multiplier, name FROM bots WHERE id = ?', (bot_id,))
        bot_config = cursor.fetchone()
        
        if not bot_config:
            logger.error(f"Bot {bot_id} not found for position import")
            return False
        
        base_size, multiplier, bot_name = bot_config
        
        # Calculate total invested (size * entry_price)
        total_invested = position_size * entry_price
        
        # Calculate the correct martingale step from position size
        calculated_step = calculate_step_from_position(total_invested, base_size, multiplier)
        
        # Calculate expected position size at this step for verification
        expected_at_step = base_size * (multiplier ** calculated_step)
        size_variance = abs(total_invested - expected_at_step) / expected_at_step if expected_at_step > 0 else 0
        
        # Log warning if position doesn't match expected step pattern
        if size_variance > 0.1:  # More than 10% variance
            logger.warning(
                f"⚠️ Position size variance for bot {bot_id} ({bot_name}): "
                f"Actual ${total_invested:.2f}, Expected at step {calculated_step}: ${expected_at_step:.2f} "
                f"(Variance: {size_variance*100:.1f}%)"
            )
        
        # Update trades table with position data using CALCULATED step
        cursor.execute('''
            UPDATE trades SET
                current_step = ?,
                total_invested = ?,
                avg_entry_price = ?,
                target_tp_price = 0,
                last_exit_price = 0,
                last_exit_time = 0,
                entry_confirmed = 1,
                basket_start_time = ?,
                entry_order_id = 'IMPORTED',
                tp_order_id = NULL,
                bot_position_id = 'IMPORTED',
                close_type = NULL
            WHERE bot_id = ?
        ''', (calculated_step, total_invested, entry_price, int(time.time()), bot_id))
        
        conn.commit()
        
        # Log the import with CORRECT step
        log_trade(
            bot_id=bot_id,
            action='POSITION_IMPORT',
            symbol=pair,
            price=entry_price,
            amount=position_size,
            cost_usdc=total_invested,
            order_id='IMPORTED',
            step=calculated_step,
            pnl=0,
            notes=f"Position imported from exchange: {position_size} @ {entry_price} (calculated step {calculated_step}, base=${base_size}, mult={multiplier})"
        )
        
        logger.info(
            f"✅ Imported position for bot {bot_id} ({bot_name}): "
            f"${total_invested:.2f} @ ${entry_price:.4f} | Step: {calculated_step} | "
            f"Base: ${base_size} | Multiplier: {multiplier}x"
        )
        return True
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to import position for bot {bot_id}: {e}")
        return False
    # Note: No conn.close() - using thread-local connection

def get_all_bots():
    """Fetches all bots status for the manager UI."""
    conn = get_connection()
    cursor = conn.cursor()
    # Return everything needed for the table
    cursor.execute('''
        SELECT b.id, b.name, b.pair, b.is_active, b.strategy_type, t.total_invested, t.current_step
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
    ''')
    results = cursor.fetchall()
    # Note: No conn.close() - using thread-local connection
    return results

def toggle_bot_active(bot_id, new_status):
    """Updates the is_active flag for a bot."""
    conn = get_connection()
    cursor = conn.cursor()
    # Ensure status is integer 0 or 1
    status_int = 1 if new_status else 0
    cursor.execute('UPDATE bots SET is_active = ? WHERE id = ?', (status_int, bot_id))
    
    # If enabling, clear any error notes from recent history that might show in UI
    # We don't delete history, but we can't easily "clear" the note from the last log entry without editing history.
    # Instead, UI logic should check if bot is currently active to decide whether to show the error.
    # The UI logic is: if not is_active, show last error.
    # So simply setting is_active=1 clears the error *display*.
    
    # However, if user re-activates, we might want to log "User Resumed".
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
    # Note: No conn.close() - using thread-local connection

def delete_bot(bot_id):
    """Deletes a bot and its trade history."""
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
    # Note: No conn.close() - using thread-local connection

def log_trade(bot_id, action, symbol, price, amount, cost_usdc=0.0, order_id=None, step=0, pnl=0.0, notes=None):
    """
    Logs a trade to the permanent trade_history table for post-mortem analysis.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO trade_history 
            (bot_id, action, symbol, price, amount, cost_usdc, order_id, step, pnl, timestamp, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (bot_id, action, symbol, price, amount, cost_usdc, order_id, step, pnl, int(time.time()), notes))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error(f"Error logging trade for bot {bot_id}: {e}")
        return None
    # Note: No conn.close() - using thread-local connection

def get_trade_history(bot_id=None, limit=100):
    """
    Fetches trade history for analysis.
    """
    conn = get_connection()
    cursor = conn.cursor()
    if bot_id:
        cursor.execute('''
            SELECT th.id, th.bot_id, b.name, th.action, th.symbol, th.price, 
                   th.amount, th.cost_usdc, th.step, th.pnl, th.timestamp, th.notes
            FROM trade_history th
            LEFT JOIN bots b ON th.bot_id = b.id
            WHERE th.bot_id = ?
            ORDER BY th.timestamp DESC
            LIMIT ?
        ''', (bot_id, limit))
    else:
        cursor.execute('''
            SELECT th.id, th.bot_id, b.name, th.action, th.symbol, th.price, 
                   th.amount, th.cost_usdc, th.step, th.pnl, th.timestamp, th.notes
            FROM trade_history th
            LEFT JOIN bots b ON th.bot_id = b.id
            ORDER BY th.timestamp DESC
            LIMIT ?
        ''', (limit,))
    return cursor.fetchall()
    # Note: No conn.close() - using thread-local connection

def get_bot_pnl_summary(bot_id):
    """
    Calculates total PnL for a bot from trade history.
    
    Returns:
        Dict with total_pnl, trade_count, win_count, loss_count
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT 
            COALESCE(SUM(pnl), 0) as total_pnl,
            COUNT(*) as trade_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count
        FROM trade_history
        WHERE bot_id = ? AND action IN ('TP_HIT', 'SELL', 'HEDGE_CLOSE', 'STOP_LOSS')
    ''', (bot_id,))
    result = cursor.fetchone()
    return {
        'total_pnl': result[0] or 0,
        'trade_count': result[1] or 0,
        'win_count': result[2] or 0,
        'loss_count': result[3] or 0
    }
    # Note: No conn.close() - using thread-local connection
    
def get_starting_equity():
    """Fetches the system's starting equity from the database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_equity WHERE key = 'STARTING_EQUITY'")
    result = cursor.fetchone()
    return result[0] if result else 10000.0

def get_system_pnl_exposure():
    """
    Fetches the system's baseline equity and total realized PnL.
    
    Returns:
        Dict with 'starting_equity', 'total_realized_pnl'
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Get Starting Equity
    cursor.execute("SELECT value FROM system_equity WHERE key = 'STARTING_EQUITY'")
    starting_equity = cursor.fetchone()
    starting_equity = starting_equity[0] if starting_equity else 10000.0
    
    # 2. Get Total Realized PnL (sum PnL from all completed trades)
    cursor.execute("SELECT COALESCE(SUM(pnl), 0) FROM trade_history")
    total_realized_pnl = cursor.fetchone()[0]

    return {
        'starting_equity': starting_equity,
        'total_realized_pnl': total_realized_pnl
    }

# ============================================
# ORDER ID TRACKING FOR MULTI-BOT SUPPORT (v0.4.1)
# ============================================

def get_order_owner(order_id):
    """
    Check which bot owns a given order ID.
    
    Returns:
        bot_id (int or None): The bot that owns this order, or None if unowned
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check bot_orders table
    cursor.execute('''
        SELECT bot_id FROM bot_orders 
        WHERE order_id = ? AND status = 'open'
        ORDER BY created_at DESC LIMIT 1
    ''', (str(order_id),))
    
    result = cursor.fetchone()
    if result:
        return result[0]
    
    # Also check trades table for entry/tp orders
    cursor.execute('''
        SELECT bot_id FROM trades 
        WHERE entry_order_id = ? OR tp_order_id = ?
        LIMIT 1
    ''', (str(order_id), str(order_id)))
    
    result = cursor.fetchone()
    if result:
        return result[0]
    
    return None

def save_bot_order(bot_id, order_type, order_id, price, amount, step=0, status='open'):
    """
    Save an exchange order ID to track which bot owns which order.
    """
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Also update the trades table for quick lookup
        if order_type == 'entry':
            cursor.execute('UPDATE trades SET entry_order_id = ? WHERE bot_id = ?', (order_id, bot_id))
        elif order_type == 'tp':
            cursor.execute('UPDATE trades SET tp_order_id = ? WHERE bot_id = ?', (order_id, bot_id))

        # Also save to detailed bot_orders table for full tracking
        cursor.execute('''
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (bot_id, step, order_type, order_id, price, amount, status, int(time.time())))

        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save bot order {order_id} (Type: {order_type}): {e}")
        try: log_trade(bot_id, 'DEBUG_ERR', 'SYS', 0, 0, 0, f"SAVE_FAIL_{order_type.upper()}", step, 0, str(e))
        except: pass

    # DEBUG TRACE ON SUCCESS
    try: log_trade(bot_id, 'DEBUG_LOG', 'SYS', price, amount, 0, f"SAVED_{order_type.upper()}", step, 0, f"Saved {order_id}")
    except: pass

def get_bot_order_ids(bot_id):
    """
    Get all order IDs for a bot.
    
    Returns:
        Dict with 'entry_order_id', 'tp_order_id', and 'grid_orders'
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get from trades table
    cursor.execute('SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id = ?', (bot_id,))
    result = cursor.fetchone()
    
    orders = {
        'entry_order_id': result[0] if result else None,
        'tp_order_id': result[1] if result else None,
        'grid_orders': []
    }
    
    # Get grid orders from bot_orders table
    cursor.execute('''
        SELECT id, order_type, order_id, price, amount, status 
        FROM bot_orders 
        WHERE bot_id = ? AND status = 'open'
    ''', (bot_id,))
    
    for row in cursor.fetchall():
        orders['grid_orders'].append({
            'id': row[0],
            'type': row[1],
            'order_id': row[2],
            'price': row[3],
            'amount': row[4],
            'status': row[5]
        })
    
    return orders

def update_order_status(order_id, status, filled_price=None):
    """
    Update order status in bot_orders table.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    if filled_price is not None:
        cursor.execute("UPDATE bot_orders SET status = ?, filled_at = ?, price = ? WHERE order_id = ?", 
                      (status, int(time.time()), filled_price, order_id))
    else:
        cursor.execute("UPDATE bot_orders SET status = ? WHERE order_id = ?", (status, order_id))
        
    conn.commit()

def update_trade_tp_price(bot_id: int, new_tp_price: float):
    """
    Updates the target TP price in the trades table.
    Used for syncing Early Exit decay or dynamic TP adjustments.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (new_tp_price, bot_id))
    conn.commit()

def get_bots_by_order_id(order_id):
    """
    Find which bot(s) own a specific order ID.
    
    Returns:
        List of bot IDs that own this order
    """
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
    """
    Match exchange orders to bots by order ID.
    
    Args:
        exchange_orders: List of order dicts from exchange (with 'id' field)
        
    Returns:
        Dict mapping order_id -> {'bot_id': X, 'bot_name': Y, 'type': Z}
    """
    order_to_bot = {}
    
    for order in exchange_orders:
        order_id = order.get('id')
        if not order_id:
            continue
        
        # Find which bot owns this order
        bots = get_bots_by_order_id(order_id)
        
        if bots:
            # Get first bot's name
            # Reuse get_connection from module scope
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


# ============================================
# BOT POSITION MANAGEMENT (v0.5.0)
# Each bot tracks its own position independently
# ============================================

def generate_bot_position_id():
    """Generate a unique ID for this bot's position tracking"""
    return str(uuid.uuid4())[:8].upper()


def get_bot_position_id(bot_id):
    """
    Get the unique position ID for a bot.
    Creates one if it doesn't exist.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT bot_position_id FROM trades WHERE bot_id = ?', (bot_id,))
    result = cursor.fetchone()
    
    if result and result[0]:
        return result[0]
    else:
        # Create new ID
        new_id = generate_bot_position_id()
        cursor.execute('UPDATE trades SET bot_position_id = ? WHERE bot_id = ?', (new_id, bot_id))
        conn.commit()
        return new_id

def update_bot_config_value(bot_id, key, value):
    """Updates a single key in the bot's config JSON."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT config FROM bots WHERE id = ?', (bot_id,))
        row = c.fetchone()
        if row:
            config_str = row[0]
            config = json.loads(config_str) if config_str else {}
            
            # Only update if changed
            if config.get(key) != value:
                config[key] = value
                new_json = json.dumps(config)
                c.execute('UPDATE bots SET config = ? WHERE id = ?', (new_json, bot_id))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Error updating config value for bot {bot_id}: {e}")
    return False


def close_bot_position(bot_id, close_type='MANUAL', close_price=0.0, close_pct=100.0, notes=None):
    """
    Close a bot's position (partial or full).
    
    Args:
        bot_id: The bot to close
        close_type: 'MANUAL', 'STOP_AFTER_PNL', 'STOP_AFTER_TIME'
        close_price: Price at which position was closed
        close_pct: Percentage of position to close (100 = full close)
        notes: Additional notes for trade history
    
    Returns:
        dict with details of the close action
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get current trade state
    cursor.execute('''
        SELECT t.total_invested, t.avg_entry_price, t.target_tp_price, 
               t.current_step, b.name, b.pair, b.direction, t.bot_position_id,
               b.config
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE t.bot_id = ?
    ''', (bot_id,))
    
    result = cursor.fetchone()
    if not result:
        conn.close()
        return {'success': False, 'error': 'Bot not found'}
    
    total_invested, avg_entry_price, target_tp, step, name, pair, direction, position_id, config_json = result
    
    if total_invested <= 0:
        conn.close()
        return {'success': False, 'error': 'No position to close'}
    
    # Calculate close amount
    close_amount = total_invested * (close_pct / 100.0)
    close_qty = close_amount / avg_entry_price if avg_entry_price > 0 else 0
    
    # Calculate PnL for this close
    if direction == 'LONG':
        pnl = (close_price - avg_entry_price) * close_qty if close_price > 0 else 0
    else:  # SHORT
        pnl = (avg_entry_price - close_price) * close_qty if close_price > 0 else 0
    
    # Log to trade history
    log_trade(
        bot_id=bot_id,
        action=f'{close_type}_CLOSE',
        symbol=pair,
        price=close_price,
        amount=close_qty,
        cost_usdc=close_amount,
        order_id=f"CLOSE_{position_id}",
        step=step,
        pnl=pnl,
        notes=notes or f"{close_type} close: {close_pct:.0f}%"
    )
    
    # Update trades table based on close percentage
    if close_pct >= 100:
        # Full close - reset to idle
        cursor.execute('''
            UPDATE trades
            SET current_step = 0,
                total_invested = 0,
                avg_entry_price = 0,
                target_tp_price = 0,
                last_exit_price = ?,
                last_exit_time = ?,
                basket_start_time = 0,
                close_type = ?
            WHERE bot_id = ?
        ''', (close_price, int(time.time()), close_type, bot_id))
    else:
        # Partial close - reduce position proportionally
        remaining_pct = 100 - close_pct
        remaining_invested = total_invested * (remaining_pct / 100.0)
        
        cursor.execute('''
            UPDATE trades
            SET total_invested = ?,
                last_exit_price = ?,
                last_exit_time = ?,
                close_type = ?
            WHERE bot_id = ?
        ''', (remaining_invested, close_price, int(time.time()), close_type, bot_id))
    
    conn.commit()
    conn.close()
    
    return {
        'success': True,
        'bot_id': bot_id,
        'bot_name': name,
        'close_type': close_type,
        'close_pct': close_pct,
        'close_price': close_price,
        'close_amount': close_amount,
        'pnl': pnl,
        'position_id': position_id
    }


def get_bot_close_settings(bot_id):
    """
    Get manual close settings for a bot from its config.
    
    Returns:
        dict with manual_close_pct, stop_after_pnl, stop_after_time
    """
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
    """
    Update manual close settings for a bot.
    
    Args:
        bot_id: The bot to update
        manual_close_pct: % of position to close when manual close triggered
        stop_after_pnl: Close when PnL reaches X USD
        stop_after_time: Close after X hours in trade
    """
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


def check_stop_after_conditions(bot_id, current_pnl, hours_in_trade):
    """
    Check if stop-after conditions are met.
    
    Args:
        bot_id: The bot to check
        current_pnl: Current unrealized PnL
        hours_in_trade: Hours since position opened
    
    Returns:
        dict with triggered conditions
    """
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


if __name__ == "__main__":
    # Self-test block
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    # Test: Create a bot
    bot_id = add_bot("RSI_Scalper_01", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
    
    if bot_id:
        print(f"Test Bot Created with ID: {bot_id}")
        
        # Test: Update Step
        update_martingale_step(bot_id, 1, 15.0, 42000.5, 43500.0)
        print("Updated to Step 1:", get_bot_status(bot_id))
        
        # Test: Reset Bot
        reset_bot_after_tp(bot_id)
        print("Reset after TP:", get_bot_status(bot_id))

def get_last_filled_order(bot_id):
    """
    Returns (price, amount, step, timestamp) of the last filled entry/grid order.
    Used for incremental grid calculation and ZOMBIE CHECK grace periods.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        # We want the highest step that is actually filled
        c.execute('''
            SELECT price, amount, step, created_at
            FROM bot_orders 
            WHERE bot_id = ? 
              AND status = 'filled' 
              AND order_type IN ('entry', 'grid')
            ORDER BY step DESC 
            LIMIT 1
        ''', (bot_id,))
        row = c.fetchone()
        if row:
            # created_at is usually proper ISO string or timestamp
            return {'price': row[0], 'amount': row[1], 'step': row[2], 'timestamp': row[3]}
    except Exception as e:
        logger.error(f"Error getting last filled order for bot {bot_id}: {e}")
    return None
