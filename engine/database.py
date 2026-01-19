import sqlite3
import os
import threading

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
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
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
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
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
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type="Martingale", config_dict=None):
    """Adds a new bot and initializes its trade state."""
    if config_dict is None:
        config_dict = {}
    
    import json
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
        print(f"Error: Bot name '{name}' already exists.")
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
    import json
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
        print(f"Error: Bot name '{name}' already exists.")
        return False
    except Exception as e:
        print(f"Error updating bot {bot_id}: {e}")
        return False
    # Note: No conn.close() - using thread-local connection

def update_martingale_step(bot_id, next_step, added_investment, new_avg_price, new_tp_price):
    """Updates the active position stats when a new Martingale step is triggered."""
    import time
    conn = get_connection()
    cursor = conn.cursor()
    
    # If step 0 (Entry), set start time if not set
    current_time = int(time.time())
    
    if next_step == 0:
        # Reset start time on fresh entry
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
    else:
        # Standard update
        cursor.execute('''
            UPDATE trades
            SET current_step = ?,
                total_invested = total_invested + ?,
                avg_entry_price = ?,
                target_tp_price = ?,
                entry_confirmed = 1
            WHERE bot_id = ?
        ''', (next_step, added_investment, new_avg_price, new_tp_price, bot_id))
    
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
        print(f"Bot {bot_id} deactivated: {reason}")
        return True
    except Exception as e:
        print(f"Failed to deactivate bot {bot_id}: {e}")
        return False

def reset_bot_after_tp(bot_id, exit_price=0):
    """Resets the trade stats after a Take Profit (TP) hit, saving exit metadata.
    IMPROVED: Now logs to trade_history with PnL calculation."""
    import time
    import logging
    logger = logging.getLogger("Database")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Fetch current trade state before resetting to calculate PnL
        cursor.execute('''
            SELECT t.total_invested, t.avg_entry_price, t.target_tp_price,
                   b.name, b.pair, b.direction, t.current_step
            FROM trades t
            JOIN bots b ON t.bot_id = b.id
            WHERE b.id = ?
        ''', (bot_id,))
        result = cursor.fetchone()

        if result:
            total_invested, avg_entry_price, target_tp_price, bot_name, pair, direction, current_step = result

            # Calculate PnL before resetting
            pnl = 0.0
            if exit_price > 0 and avg_entry_price > 0 and total_invested > 0:
                # Estimate quantity (in base currency)
                # Crypto: Base/Quote, Investment is Quote
                # Qty = Investment / Price
                est_qty = total_invested / avg_entry_price

                if direction.upper() == 'LONG':
                    pnl = (exit_price - avg_entry_price) * est_qty
                else:  # SHORT
                    pnl = (avg_entry_price - exit_price) * est_qty

                logger.info(f"TP PnL for {bot_name}: Exit=${exit_price:.4f}, Entry=${avg_entry_price:.4f}, PnL=${pnl:.2f}")

            # Log the TP hit to trade_history BEFORE resetting
            log_trade(
                bot_id=bot_id,
                action='TP_HIT',
                symbol=pair,
                price=exit_price,
                amount=total_invested / avg_entry_price if avg_entry_price > 0 else 0,
                cost_usdc=total_invested,
                step=current_step,
                pnl=pnl,
                notes=f"TP hit at step {current_step}, avg entry {avg_entry_price:.4f}"
            )
            logger.info(f"Logged TP_HIT to trade_history for {bot_name}")

        # Reset the trade state
        cursor.execute('''
            UPDATE trades
            SET current_step = 0,
                total_invested = 0,
                avg_entry_price = 0,
                target_tp_price = 0,
                last_exit_price = ?,
                last_exit_time = ?,
                basket_start_time = 0
            WHERE bot_id = ?
        ''', (exit_price, int(time.time()), bot_id))
        conn.commit()
        logger.info(f"Reset trade state for bot {bot_id} at exit price {exit_price:.4f}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise
    # Note: No conn.close() - using thread-local connection

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
        print(f"Error deleting bot {bot_id}: {e}")
        return False
    # Note: No conn.close() - using thread-local connection

def log_trade(bot_id, action, symbol, price, amount, cost_usdc=0, order_id=None, step=0, pnl=0, notes=None):
    """
    Logs a trade to the permanent trade_history table for post-mortem analysis.
    
    Args:
        bot_id: The bot that executed this trade
        action: 'BUY', 'SELL', 'TP_HIT', 'HEDGE_OPEN', 'HEDGE_CLOSE', 'STOP_LOSS'
        symbol: Trading pair (e.g., 'BTC/USDC')
        price: Execution price
        amount: Position size in base currency
        cost_usdc: Total cost in USDC
        order_id: Exchange order ID (optional)
        step: Martingale step number
        pnl: Realized PnL for closing trades
        notes: Additional context (optional)
    """
    import time
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
        print(f"Error logging trade for bot {bot_id}: {e}")
        return None
    # Note: No conn.close() - using thread-local connection

def get_trade_history(bot_id=None, limit=100):
    """
    Fetches trade history for analysis.
    
    Args:
        bot_id: Filter by bot (None = all bots)
        limit: Max records to return
    
    Returns:
        List of trade records
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

# ============================================
# ORDER ID TRACKING FOR MULTI-BOT SUPPORT (v0.4.1)
# ============================================

def save_bot_order(bot_id, order_type, order_id, price, amount):
    """
    Save an exchange order ID to track which bot owns which order.
    
    Args:
        bot_id: The bot that placed the order
        order_type: 'entry', 'tp', 'grid'
        order_id: The exchange order ID
        price: Order price
        amount: Order amount
    """
    import time
    conn = get_connection()
    cursor = conn.cursor()
    
    # Also update the trades table for quick lookup
    if order_type == 'entry':
        cursor.execute('UPDATE trades SET entry_order_id = ? WHERE bot_id = ?', (order_id, bot_id))
    elif order_type == 'tp':
        cursor.execute('UPDATE trades SET tp_order_id = ? WHERE bot_id = ?', (order_id, bot_id))
    
    # Also save to detailed bot_orders table for full tracking
    cursor.execute('''
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
    ''', (bot_id, order_type, order_id, price, amount, int(time.time())))
    
    conn.commit()

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
    
    Args:
        order_id: The exchange order ID
        status: 'open', 'filled', 'cancelled'
        filled_price: Price if filled
    """
    import time
    conn = get_connection()
    cursor = conn.cursor()
    
    if filled_price:
        cursor.execute('''
            UPDATE bot_orders 
            SET status = ?, filled_at = ?
            WHERE order_id = ?
        ''', (status, int(time.time()), order_id))
    else:
        cursor.execute('''
            UPDATE bot_orders 
            SET status = ?
            WHERE order_id = ?
        ''', (status, order_id))
    
    conn.commit()

def get_bots_by_order_id(order_id):
    """
    Find which bot(s) own a specific order ID.
    
    Args:
        order_id: The exchange order ID
        
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
            from .database import get_connection
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

if __name__ == "__main__":
    # Self-test block
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
