import sqlite3
import os
import threading
import json
import time
import logging
import uuid
from typing import List, Dict, Any, Tuple

# Setup logger
logger = logging.getLogger(__name__)

# Use absolute path to ensure database is found regardless of working directory


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "crypto_bot.db")

# Thread-local storage for SQLite connections
# SQLite connections should not be shared across threads
_local = threading.local()

def get_connection():
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
    if hasattr(_local, 'connection') and _local.connection:
        try:
            _local.connection.close()
        except Exception:
            pass
        _local.connection = None

def get_starting_equity():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_equity WHERE key = 'STARTING_EQUITY'")
    row = cursor.fetchone()
    return float(row[0]) if row else 10000.0

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
        try:
            cursor.execute('SELECT entry_order_id FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN entry_order_id TEXT')
            cursor.execute('ALTER TABLE trades ADD COLUMN tp_order_id TEXT')
            conn.commit()

        # Migration for independent position tracking (v0.5.0)
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

        # Create separate table for grid orders
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
        
        # Trade history table
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
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_time ON trade_history(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_active ON bots(is_active)')

        # Notifications table
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

        # Active positions table (what's on the exchange, per bot virtual position)
        # Re-create to ensure schema update (safe as it's just a cache)
        cursor.execute('DROP TABLE IF EXISTS active_positions')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_positions (
                bot_id INTEGER NOT NULL,
                pair TEXT,
                side TEXT,
                size REAL NOT NULL DEFAULT 0,
                entry_price REAL DEFAULT 0,
                last_checked INTEGER,
                last_updated INTEGER DEFAULT (datetime('now')),
                PRIMARY KEY (bot_id, pair, side)
            )
        ''')

        # FUNDAMENTAL FIX: Clear stale active positions on startup
        # This prevents the UI from showing "Green" (Synced) against old data before the first poll cycle
        cursor.execute('DELETE FROM active_positions')

        conn.commit()
    except Exception as e:
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
    try:
        # logger.info(f"🔍 [DIAG-NOTIFICATION] Adding notification: type={type}, bot_id={bot_id}, message={message[:50]}...")
        
        conn = get_connection()
        
        # CRITICAL FIX: Use IMMEDIATE transaction to prevent race conditions across processes
        # This locks the DB for writing, ensuring the SELECT-then-INSERT is atomic regarding other writers
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # If already in transaction, we proceed (connection isolation should handle it or we accept risk)
            pass
            
        cursor = conn.cursor()
        
        current_time = time.time()
        dedup_window = 10.0  # Increased to 10s to catch lagging duplicates
        
        # Check for exact duplicate
        cursor.execute("""
            SELECT COUNT(*) FROM notifications 
            WHERE type = ? AND message = ? AND bot_id = ? 
            AND timestamp > ?
        """, (type, message, bot_id, current_time - dedup_window))
        
        duplicate_count = cursor.fetchone()[0]
        
        if duplicate_count > 0:
            logger.warning(f"🔍 [DIAG-NOTIFICATION] Duplicate notification detected and BLOCKED")
            conn.commit() # Release lock
            return  # Skip insertion
        
        # No duplicate found - insert notification
        conn.execute(
            "INSERT INTO notifications (timestamp, type, message, bot_id) VALUES (?, ?, ?, ?)",
            (current_time, type, message, bot_id)
        )
        conn.commit()
        # logger.info(f"🔍 [DIAG-NOTIFICATION] Notification added successfully")
    except Exception as e:
        logger.error(f"Failed to add notification: {e}")
        try:
            conn = get_connection()
            conn.rollback()
        except:
            pass

def get_unread_notifications(limit=10):
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
    if not notification_ids: return
    try:
        conn = get_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(notification_ids))
        sql = f"UPDATE notifications SET is_read = 1 WHERE id IN ({placeholders})"
        # logger.info(f"DEBUG: Marking notifications read: {notification_ids} using SQL: {sql}")
        cursor.execute(sql, notification_ids)
        rowcount = cursor.rowcount
        conn.commit()
        # if rowcount == 0:
        #     logger.warning(f"⚠️ Mark read affected 0 rows for IDs: {notification_ids}")
        # else:
        #     logger.info(f"✅ Marked {rowcount} notifications as read. IDs: {notification_ids}")
            
    except Exception as e:
        logger.error(f"Failed to mark notifications read: {e}")

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type="Martingale", config_dict=None):
    if config_dict is None:
        config_dict = {}
    config_json = json.dumps(config_dict)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO bots (name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json))
        bot_id = cursor.lastrowid
        cursor.execute('INSERT INTO trades (bot_id) VALUES (?)', (bot_id,))
        conn.commit()
        return bot_id
    except sqlite3.IntegrityError:
        logger.warning(f"Error: Bot name '{name}' already exists.")
        return None

def get_bot_params(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config FROM bots WHERE id = ?', (bot_id,))
    return cursor.fetchone()

def update_bot_config_value(bot_id, key, value):
    """Parses JSON config, updates a single key, and saves back."""
    try:
        params = get_bot_params(bot_id)
        if not params or not params[7]:
            config_dict = {}
        else:
            config_dict = json.loads(params[7])
            
        config_dict[key] = value
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE bots SET config = ? WHERE id = ?", (json.dumps(config_dict), bot_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update bot config value for {bot_id}: {e}")
        return False

def get_bot_status(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT b.id, b.name, b.pair, 
               COALESCE(t.current_step, 0) as current_step,
               COALESCE(t.total_invested, 0) as total_invested, 
               COALESCE(t.avg_entry_price, 0) as avg_entry_price,
               COALESCE(t.target_tp_price, 0) as target_tp_price,
               COALESCE(t.last_exit_price, 0) as last_exit_price,
               COALESCE(t.last_exit_time, 0) as last_exit_time,
               COALESCE(t.basket_start_time, 0) as basket_start_time,
               b.direction, b.is_active
        FROM bots b 
        LEFT JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = ?
    """, (bot_id,))
    row = cursor.fetchone()
    if not row:
        return None
    
    result = {
        'id': row[0],
        'name': row[1],
        'pair': row[2],
        'current_step': row[3],
        'total_invested': row[4],
        'avg_entry_price': row[5],
        'target_tp_price': row[6],
        'last_exit_price': row[7],
        'last_exit_time': row[8],
        'basket_start_time': row[9],
        'direction': row[10],
        'is_active': row[11]
    }

    # --- SAFETY GUARD: Prevent Phantom/Corrupted State Usage ---
    # Relaxed for modern market conditions and Short triggers
    if result['pair'] and 'BTC' in result['pair'] and result['avg_entry_price'] > 0 and result['avg_entry_price'] < 10000:
        logger.critical(f"🛡️ SAFETY GUARD: Bot {result['name']} has CORRUPTED Entry Price (${result['avg_entry_price']:.2f}). Forcing IDLE state override.")
        result['total_invested'] = 0
        result['avg_entry_price'] = 0
        result['current_step'] = 0
        result['entry_order_id'] = None
        result['tp_order_id'] = None
        
    return result

def update_bot(bot_id, name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config_dict):
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

def update_martingale_step(bot_id, step, total_invested, avg_price, tp_price):
    """Updates or inserts the trade state for a specific bot (UPSERT logic)."""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Check if trade record exists
        cursor.execute("SELECT current_step FROM trades WHERE bot_id = ?", (bot_id,))
        exists = cursor.fetchone()
        
        if exists:
            # UPDATE existing record
            cursor.execute("""
                UPDATE trades 
                SET current_step = ?, 
                    total_invested = ?, 
                    avg_entry_price = ?, 
                    target_tp_price = ?,
                    entry_confirmed = 1,
                    basket_start_time = CASE WHEN basket_start_time = 0 THEN ? ELSE basket_start_time END
                WHERE bot_id = ?
            """, (step, total_invested, avg_price, tp_price, int(time.time()), bot_id))
            logger.debug(f"✅ Updated trade state for bot {bot_id}: step={step}, invested={total_invested}, avg_price={avg_price}")
        else:
            # INSERT new record
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price, entry_confirmed, basket_start_time)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (bot_id, step, total_invested, avg_price, tp_price, int(time.time())))
            logger.info(f"✅ Created trade record for bot {bot_id}: step={step}, invested={total_invested}, avg_price={avg_price}")
        
        cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update martingale step for bot {bot_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        return False

def deactivate_bot(bot_id, reason="Unknown Error"):
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("UPDATE bots SET is_active = 0, status='STOPPED' WHERE id = ?", (bot_id,))
        log_trade(bot_id, 'ERROR_STOP', 'SYSTEM', 0, 0, 0, "SYS_STOP", 0, f"Auto-Stopped: {reason}")
        conn.commit()
        logger.info(f"Bot {bot_id} deactivated: {reason}")
        return True
    except Exception as e:
        logger.error(f"Failed to deactivate bot {bot_id}: {e}")
        return False

def calculate_step_from_position(position_size: float, base_size: float, multiplier: float) -> int:
    if position_size <= 0 or base_size <= 0 or multiplier <= 1:
        return 0
    ratio = position_size / base_size
    if ratio <= 1:
        return 0
    import math
    step = math.log(ratio) / math.log(multiplier)
    return max(0, round(step))

def reset_bot_after_tp(bot_id, exit_price, direction=None, action_label='TP_HIT', notes=''):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT total_invested, current_step, avg_entry_price, name, pair, direction FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row: return
        total_invested, current_step, avg_entry_price, bot_name, pair, db_direction = row
        
        # Use provided direction or fallback to DB direction
        final_direction = direction or db_direction or 'LONG'
        
        pnl = 0.0
        if exit_price > 0 and avg_entry_price > 0:
            est_qty = total_invested / avg_entry_price
            if final_direction.upper() == 'LONG':
                pnl = (exit_price - avg_entry_price) * est_qty
            else:
                pnl = (avg_entry_price - exit_price) * est_qty
        log_trade(bot_id, action_label, pair, exit_price, total_invested / avg_entry_price if avg_entry_price > 0 else 0, total_invested, step=current_step, pnl=pnl, notes=notes)
        cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = 0, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = ? WHERE bot_id = ?", (exit_price, int(time.time()), action_label, bot_id))
        cursor.execute("UPDATE bot_orders SET status = 'auto_closed', updated_at = ? WHERE bot_id = ? AND status = 'open'", (int(time.time()), bot_id))
        cursor.execute("UPDATE bots SET status='Scanning' WHERE id = ?", (bot_id,))
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except: pass
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise

def check_and_fix_integrity():
    """
    Sanitizes the database state on startup (or periodically).
    Fixes 'Zombie' (In Trade but 0 invested) and 'Ghost' (Scanning but invested) states.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT b.id, b.name, b.status, t.total_invested FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1")
    rows = cursor.fetchall()
    
    fixed_count = 0
    for row in rows:
        bot_id, name, status, invested = row
        invested = invested or 0
        
        # Case 1: Zombie State (Active Status but No Money)
        # Status "IN TRADE" (or similar) but invested is 0
        if status and status.upper() in ['IN TRADE', 'TRADING'] and invested <= 0:
            logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is '{status}' but has $0 invested. Resetting to 'Scanning'.")
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
            fixed_count += 1
            
        # Case 2: Ghost State (Scanning Status but Money trapped)
        # Status "Scanning" but invested > 0
        elif status and status.upper() == 'SCANNING' and invested > 1.0: # Ignore dust < $1
            logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is '{status}' but has ${invested:.2f} invested. Updating to 'IN TRADE'.")
            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
            fixed_count += 1

    # Also fix any legacy "Waiting for Signal" rows to "Scanning" standard
    cursor.execute("UPDATE bots SET status='Scanning' WHERE status='Waiting for Signal'")
    
    # Fix any bots with NULL status
    cursor.execute("UPDATE bots SET status='Scanning' WHERE status IS NULL OR status = ''")
    
    conn.commit()
    if fixed_count > 0:
        logger.info(f"✅ DB Integrity Check: Fixed {fixed_count} bot states.")

def update_bot_display_status(bot_id: int, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE bots SET status = ? WHERE id = ?', (status, bot_id))
    conn.commit()

def get_bot_order_ids(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    orders = {'entry_order_id': None, 'tp_order_id': None, 'grid_orders': []}
    
    # Primary source: trades table (fast, single row)
    cursor.execute('SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id = ?', (bot_id,))
    res = cursor.fetchone()
    if res:
        orders['entry_order_id'], orders['tp_order_id'] = res
    
    # BELT-AND-SUSPENDERS: If trades table has NULL, check bot_orders as fallback.
    # This covers the window between save_bot_order() inserting into bot_orders
    # and the trades table being updated (shouldn't happen now, but defensive).
    if not orders['entry_order_id']:
        cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'entry' AND status = 'open' ORDER BY created_at DESC LIMIT 1", (bot_id,))
        entry_row = cursor.fetchone()
        if entry_row and entry_row[0]:
            orders['entry_order_id'] = entry_row[0]
    
    if not orders['tp_order_id']:
        cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'tp' AND status = 'open' ORDER BY created_at DESC LIMIT 1", (bot_id,))
        tp_row = cursor.fetchone()
        if tp_row and tp_row[0]:
            orders['tp_order_id'] = tp_row[0]
    
    # Grid orders from bot_orders (filter to grid type only)
    cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'grid' AND status = 'open'", (bot_id,))
    orders['grid_orders'] = [{'order_id': r[0]} for r in cursor.fetchall() if r[0]]
    return orders

def import_position_from_exchange(bot_id: int, pair: str, position_size: float, entry_price: float, direction: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT direction, martingale_multiplier, base_size, config FROM bots WHERE id = ?", (bot_id,))
    params = cursor.fetchone()
    if not params: return False
    bot_direction, multiplier, base_size, config_json = params

    if direction.upper() != bot_direction.upper(): return False
    
    total_invested = abs(float(position_size)) * float(entry_price)

    # FUNDAMENTAL FIX: Calculate and save target_tp_price on adoption
    import json
    from engine.runner import BotRunner # Mildly circular, but necessary for strategy access
    from engine.strategies.martingale_strategy import MartingaleStrategy
    
    runner_instance = BotRunner.get_instance()
    if runner_instance:
        bot_params = json.loads(config_json) if config_json else {}
        strategy = runner_instance.get_strategy(bot_id, bot_params)
        side = 'buy' if direction.upper() == 'LONG' else 'sell'
        tp_price = strategy.calculate_tp_price(entry_price=float(entry_price), current_step=0, side=side)
    else:
        # Fallback if runner isn't available (e.g., standalone script)
        tp_price = float(entry_price) * 1.015 if direction.upper() == 'LONG' else float(entry_price) * 0.985

    calculated_step = 0
    if base_size > 0 and multiplier > 1 and total_invested > base_size:
        import math
        try:
            calculated_step = round(math.log(total_invested / base_size) / math.log(multiplier))
        except: pass
    try:
        # Check if record exists (UPSERT logic)
        cursor.execute("SELECT current_step FROM trades WHERE bot_id = ?", (bot_id,))
        exists = cursor.fetchone()
        
        if exists:
            # UPDATE existing record
            cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=?, target_tp_price=?, basket_start_time=?, entry_confirmed=1 WHERE bot_id=?", 
                           (max(0, calculated_step), total_invested, float(entry_price), tp_price, int(time.time()), bot_id))
            logger.debug(f"import_position_from_exchange: UPDATED bot {bot_id}")
        else:
            # INSERT new record
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price, basket_start_time, entry_confirmed)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (bot_id, max(0, calculated_step), total_invested, float(entry_price), tp_price, int(time.time())))
            logger.debug(f"import_position_from_exchange: INSERTED bot {bot_id}")
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"import_position_from_exchange failed for bot {bot_id}: {e}")
        return False

def get_all_bots():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT b.id, b.name, b.pair, b.is_active, b.strategy_type, COALESCE(t.total_invested, 0), COALESCE(t.current_step, 0) FROM bots b LEFT JOIN trades t ON b.id = t.bot_id")
    bots = cursor.fetchall()
    logger.critical(f"🔍 [GET_ALL_BOTS] Query returned {len(bots)} bots from DB.")
    for bot in bots:
        logger.critical(f"   - DB Bot Raw: ID={bot[0]}, Name={bot[1]}, Active={bot[3]}")
    return bots

def toggle_bot_active(bot_id, new_status):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE bots SET is_active = ? WHERE id = ?', (1 if new_status else 0, bot_id))
    conn.commit()

def delete_bot(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))
        cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
        cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()
        return True
    except Exception as e:
        return False

def confirm_order(db_id, exchange_order_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bot_orders SET order_id = ?, status = 'open', updated_at = ? WHERE id = ?", (exchange_order_id, int(time.time()), db_id))
        conn.commit()
        return True
    except: return False

def fail_order(db_id, reason):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bot_orders SET status = 'failed', notes = ?, updated_at = ? WHERE id = ?", (reason, int(time.time()), db_id))
        conn.commit()
        return True
    except: return False

def cleanup_pending_orders(exchange):
    conn = get_connection()
    cursor = conn.cursor()
    threshold_time = int(time.time()) - 30
    try:
        cursor.execute("SELECT id, bot_id, order_type, client_order_id FROM bot_orders WHERE status = 'open' AND created_at < ?", (threshold_time,))
        pending = cursor.fetchall()
        if not pending: return {'total': 0}
        ex_orders = exchange.exchange.fetch_open_orders()
        ex_by_client_id = {o.get('clientOrderId', ''): o for o in ex_orders if o.get('clientOrderId')}
        for db_id, bot_id, order_type, client_id in pending:
            if client_id in ex_by_client_id:
                confirm_order(db_id, ex_by_client_id[client_id]['id'])
            else:
                fail_order(db_id, 'Not found on exchange')
        return {'total': len(pending)}
    except: return {'total': 0}

def update_active_positions_snapshot(positions: list):
    """
    Updates the active_positions table with the latest snapshot from the exchange.
    This provides the UI with a real-time view of 'Physical Reality'.
    """
    conn = None
    try:
        conn = get_connection()
        # Use a transaction to ensure atomicity
        conn.execute("BEGIN IMMEDIATE")
        
        # Clear old snapshot
        conn.execute("DELETE FROM active_positions")
        
        for p in positions:
            raw_symbol = p.get('symbol', 'UNKNOWN')
            # Normalize symbol to match DB format (BTCUSDC -> BTC/USDC)
            # This is critical for the UI to match Virtual vs Physical
            if raw_symbol.endswith('USDC'):
                symbol = f"{raw_symbol[:-4]}/USDC"
            elif raw_symbol.endswith('USDT'):
                symbol = f"{raw_symbol[:-4]}/USDT"
            else:
                symbol = raw_symbol

            amount = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            side = p.get('side', 'LONG').upper()
            entry_price = float(p.get('entryPrice', 0))
            
            if amount != 0:
                # We use bot_id=0 to represent 'Physical Exchange' (no specific bot owner implied here)
                conn.execute("""
                    INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                    VALUES (0, ?, ?, ?, ?, ?)
                """, (symbol, side, amount, entry_price, int(time.time())))
        
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update active_positions snapshot: {e}")
        if conn:
            try:
                conn.rollback()
            except: pass

def update_trade_tp_price(bot_id: int, new_tp_price: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (new_tp_price, bot_id))
    conn.commit()

def get_bots_by_order_id(order_id):
    conn = get_connection()
    cursor = conn.cursor()
    bot_ids = []
    cursor.execute('SELECT bot_id FROM trades WHERE entry_order_id = ? OR tp_order_id = ?', (order_id, order_id))
    for row in cursor.fetchall(): bot_ids.append({'bot_id': row[0], 'type': 'trade'})
    cursor.execute('SELECT bot_id, order_type FROM bot_orders WHERE order_id = ?', (order_id,))
    for row in cursor.fetchall(): bot_ids.append({'bot_id': row[0], 'type': row[1]})
    return bot_ids

def get_order_owner(order_id):
    """Finds the bot_id that owns a specific order_id."""
    bots = get_bots_by_order_id(order_id)
    if bots:
        return bots[0]['bot_id']
    return None

def match_exchange_orders_to_bots(exchange_orders):
    order_to_bot = {}
    for order in exchange_orders:
        order_id = order.get('id')
        if not order_id: continue
        bots = get_bots_by_order_id(order_id)
        if bots:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT name FROM bots WHERE id = ?', (bots[0]['bot_id'],))
            res = cursor.fetchone()
            order_to_bot[order_id] = {'bot_id': bots[0]['bot_id'], 'bot_name': res[0] if res else 'Unknown', 'type': bots[0]['type'], 'order_info': order}
        else:
            order_to_bot[order_id] = {'bot_id': None, 'bot_name': 'MANUAL', 'type': 'unknown', 'order_info': order}
    return order_to_bot

def generate_bot_position_id():
    return str(uuid.uuid4())[:8].upper()

def close_bot_position(bot_id, close_type='MANUAL', close_price=0.0, close_pct=100.0, notes=''):
    """
    Closes or partially closes a bot's position in the database.
    Restored from missing implementation.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Get current state
        cursor.execute("SELECT pair, direction, total_invested, avg_entry_price FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row:
            return {'success': False, 'error': 'Bot not found'}
        
        pair, direction, total_invested, avg_entry = row
        
        if close_pct >= 100:
            # Full close
            reset_bot_after_tp(bot_id, exit_price=close_price, direction=direction, action_label=close_type, notes=notes)
            return {'success': True, 'status': 'Fully Closed'}
        else:
            # Partial close - just reduce total_invested and log
            reduction = total_invested * (close_pct / 100.0)
            new_invested = max(0, total_invested - reduction)
            
            cursor.execute("UPDATE trades SET total_invested = ? WHERE bot_id = ?", (new_invested, bot_id))
            conn.commit()
            
            log_trade(bot_id, f'PARTIAL_{close_type}', pair, close_price, reduction / close_price if close_price > 0 else 0, reduction, notes=notes)
            return {'success': True, 'status': f'Partially Closed ({close_pct}%)'}
            
    except Exception as e:
        logger.error(f"Error in close_bot_position for bot {bot_id}: {e}")
        return {'success': False, 'error': str(e)}

def get_bot_position_id(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT bot_position_id FROM trades WHERE bot_id = ?', (bot_id,))
    res = cursor.fetchone()
    return res[0] if res else None

def reconcile_with_db(bot_id, current_price, open_orders, exchange_position):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT t.total_invested, b.pair FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
    res = cursor.fetchone()
    if not res: return {'success': False}
    total_invested, pair = res
    if not exchange_position or float(exchange_position.get('size', 0)) == 0:
        if total_invested > 0:
            cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = 0, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = 'RECONCILE' WHERE bot_id = ?", (current_price, bot_id))
            cursor.execute("UPDATE bot_orders SET status = 'auto_closed', updated_at = ? WHERE bot_id = ? AND status = 'open'", (int(time.time()), bot_id))
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id = ?", (bot_id,))
    conn.commit()
    return {'success': True}

def get_bot_close_settings(bot_id):
    params = get_bot_params(bot_id)
    if not params: return None
    config_dict = json.loads(params[7]) if params[7] else {}
    return {'manual_close_pct': config_dict.get('manual_close_pct', 100.0), 'stop_after_pnl': config_dict.get('stop_after_pnl', 0.0), 'stop_after_time': config_dict.get('stop_after_time', 0)}

def update_bot_close_settings(bot_id, manual_close_pct=None, stop_after_pnl=None, stop_after_time=None):
    """Update bot close settings in config JSON"""
    try:
        params = get_bot_params(bot_id)
        if not params: return False
        config_dict = json.loads(params[7]) if params[7] else {}
        
        if manual_close_pct is not None:
            config_dict['manual_close_pct'] = manual_close_pct
        if stop_after_pnl is not None:
            config_dict['stop_after_pnl'] = stop_after_pnl
        if stop_after_time is not None:
            config_dict['stop_after_time'] = stop_after_time
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE bots SET config = ? WHERE id = ?", (json.dumps(config_dict), bot_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update bot close settings for {bot_id}: {e}")
        return False

def check_stop_after_conditions(bot_id, current_pnl, hours_in_trade):
    """Check if bot should stop based on PnL or time conditions"""
    settings = get_bot_close_settings(bot_id)
    if not settings:
        return {'triggered': False, 'conditions': []}
    
    stop_pnl = settings.get('stop_after_pnl', 0.0)
    stop_time = settings.get('stop_after_time', 0)
    
    conditions = []
    
    if stop_pnl > 0 and current_pnl >= stop_pnl:
        conditions.append({
            'type': 'PNL_TARGET',
            'message': f"PnL target reached: ${current_pnl:.2f} >= ${stop_pnl:.2f}"
        })
    
    if stop_time > 0 and hours_in_trade >= stop_time:
        conditions.append({
            'type': 'TIME_LIMIT',
            'message': f"Time limit reached: {hours_in_trade:.1f}h >= {stop_time}h"
        })
    
    return {
        'triggered': len(conditions) > 0,
        'conditions': conditions
    }

def get_bot_pnl_summary(bot_id):
    """Get PnL summary for a bot from trade history"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Sum all PnL from trade_history for this bot
        cursor.execute("""
            SELECT 
                COALESCE(SUM(pnl), 0.0) as total_pnl,
                COUNT(*) as trade_count,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0.0) as winning_pnl,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0.0) as losing_pnl,
                COUNT(CASE WHEN pnl > 0 THEN 1 END) as wins,
                COUNT(CASE WHEN pnl < 0 THEN 1 END) as losses
            FROM trade_history
            WHERE bot_id = ?
        """, (bot_id,))
        
        row = cursor.fetchone()
        if not row:
            return {
                'total_pnl': 0.0,
                'trade_count': 0,
                'winning_pnl': 0.0,
                'losing_pnl': 0.0,
                'wins': 0,
                'losses': 0,
                'win_rate': 0.0
            }
        
        total_pnl, trade_count, winning_pnl, losing_pnl, wins, losses = row
        win_rate = (wins / trade_count * 100) if trade_count > 0 else 0.0
        
        return {
            'total_pnl': float(total_pnl or 0.0),
            'trade_count': int(trade_count or 0),
            'winning_pnl': float(winning_pnl or 0.0),
            'losing_pnl': float(losing_pnl or 0.0),
            'wins': int(wins or 0),
            'losses': int(losses or 0),
            'win_rate': float(win_rate)
        }
    except Exception as e:
        logger.error(f"Failed to get PnL summary for bot {bot_id}: {e}")
        return {
            'total_pnl': 0.0,
            'trade_count': 0,
            'winning_pnl': 0.0,
            'losing_pnl': 0.0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0.0
        }

def log_trade(bot_id, action, symbol, price, amount, cost_usdc, order_id="UNKNOWN", step=0, notes="", pnl=0.0):
    try:
        conn = get_connection()
        # Robust conversion for incorrect argument types from bot_executor
        if isinstance(notes, (int, float)): notes = str(notes)
        if isinstance(pnl, str): 
            try: pnl = float(pnl)
            except: pnl = 0.0
            
        conn.execute("""
            INSERT INTO trade_history (bot_id, timestamp, action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bot_id, int(time.time()), action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to log trade: {e}")
        return False

def get_trade_history(bot_id=None, limit=100):
    conn = get_connection()
    c = conn.cursor()
    if bot_id: c.execute("SELECT * FROM trade_history WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?", (bot_id, limit))
    else: c.execute("SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT ?", (limit,))
    return c.fetchall()

def save_bot_order(bot_id, order_type, exchange_order_id, price, amount, step, status='open', client_order_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, client_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (bot_id, step, order_type, exchange_order_id, price, amount, status, int(time.time()), client_order_id))
        
        # CRITICAL FIX: Also update trades table so get_bot_order_ids() / Guardian can see them.
        # Previously only bot_orders was written, but Guardian reads trades.entry_order_id/tp_order_id.
        if order_type == 'entry':
            cursor.execute("UPDATE trades SET entry_order_id = ? WHERE bot_id = ?", (exchange_order_id, bot_id))
        elif order_type == 'tp':
            cursor.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (exchange_order_id, bot_id))
        
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save bot order: {e}")
        return False

def update_order_status(order_id, status, bot_id=None):
    """Updates the status of an order in the database."""
    conn = get_connection()
    if bot_id:
        conn.execute("UPDATE bot_orders SET status = ? WHERE order_id = ? AND bot_id = ?", (status, order_id, bot_id))
    else:
        conn.execute("UPDATE bot_orders SET status = ? WHERE order_id = ?", (status, order_id))
    conn.commit()

def close_order_in_db(order_id):
    try:
        conn = get_connection()
        conn.execute("UPDATE bot_orders SET status = 'closed', updated_at = ? WHERE order_id = ?", (int(time.time()), order_id))
        conn.commit()
        return True
    except: return False

def get_all_active_trades_for_pair(pair: str):
    """
    Retrieves all active trades for a given pair.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.*, b.name, b.direction
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE b.pair = ? AND t.total_invested > 0 AND b.is_active = 1
    """, (pair,))
    rows = cursor.fetchall()
    
    # Get column names from cursor description
    column_names = [description[0] for description in cursor.description]
    
    # Convert list of tuples to list of dictionaries
    trades = [dict(zip(column_names, row)) for row in rows]
    
    return trades

def get_last_filled_order(bot_id):
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT basket_start_time FROM trades WHERE bot_id = ?", (bot_id,))
        res = c.fetchone()
        basket_start_time = res[0] if res else 0
        c.execute("SELECT price, amount, step, created_at FROM bot_orders WHERE bot_id = ? AND order_type = 'buy' AND status = 'filled' AND created_at >= ? ORDER BY created_at DESC LIMIT 1", (bot_id, basket_start_time))
        row = c.fetchone()
        if row: return {'price': row[0], 'amount': row[1], 'step': row[2], 'timestamp': row[3]}
    except: pass
    return None

def update_full_snapshot(trade_updates: List[Dict[str, Any]], physical_positions: List[Dict[str, Any]]):
    """
    Atomically updates both the virtual (trades) and physical (active_positions) state.
    This is the fundamental fix to prevent UI race conditions.
    """
    conn = None
    try:
        conn = get_connection()
        # check if there are active connect before attempting to connect
        if not conn: return
        conn.execute("BEGIN IMMEDIATE")
        
        # 1. Update Virtual Positions
        for update in trade_updates:
            bot_id = update['bot_id']
            # Using UPSERT logic for robustness
            conn.execute("""
                INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, basket_start_time)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    total_invested = excluded.total_invested,
                    avg_entry_price = excluded.avg_entry_price,
                    entry_confirmed = excluded.entry_confirmed,
                    basket_start_time = CASE WHEN basket_start_time = 0 THEN ? ELSE basket_start_time END
                WHERE bot_id = ?
            """, (bot_id, update['total_invested'], update['avg_entry_price'], update['entry_confirmed'], update['basket_start_time'], int(time.time()), bot_id))

        # 2. Update Physical Positions
        conn.execute("DELETE FROM active_positions")
        for p in physical_positions:
            # Symbol normalization logic from previous fixes
            raw_symbol = p.get('symbol', 'UNKNOWN')
            if raw_symbol.endswith('USDC'): symbol = f"{raw_symbol[:-4]}/USDC"
            elif raw_symbol.endswith('USDT'): symbol = f"{raw_symbol[:-4]}/USDT"
            else: symbol = raw_symbol
            
            amount = float(p.get('contracts', 0))
            side = 'LONG' if amount > 0 else 'SHORT'
            entry_price = float(p.get('entryPrice', 0))
            
            if amount != 0:
                # We use bot_id=0 to represent 'Physical Exchange' (no specific bot owner implied here)
                conn.execute("""
                    INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                    VALUES (0, ?, ?, ?, ?, ?)
                """, (symbol, side, amount, entry_price, int(time.time())))
        
        conn.commit()
        logger.info("✅ Transactional snapshot update complete.")
    except Exception as e:
        logger.error(f"Failed to execute transactional snapshot update: {e}")
        if conn:
            try:
                conn.rollback()
            except: pass
