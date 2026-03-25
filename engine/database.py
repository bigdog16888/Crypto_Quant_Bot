import sqlite3
import os
import threading
import json
import time
import logging
import uuid
import shutil
import datetime
from typing import List, Dict, Any, Tuple, Optional

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
        # ENABLE WAL MODE for enterprise concurrency safety
        _local.connection.execute("PRAGMA journal_mode=WAL")
        _local.connection.execute("PRAGMA synchronous=NORMAL")
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

def backup_database():
    """Creates a timestamped backup of the database before modification."""
    if not os.path.exists(DB_PATH):
        return
        
    try:
        backup_dir = os.path.join(BASE_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"crypto_bot_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_name)
        
        # Use shutil for safe copy
        shutil.copy2(DB_PATH, backup_path)
        logger.info(f"✅ Database backed up to: {backup_path}")
        
        # Cleanup old backups (keep last 10)
        backups = sorted([os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')], key=os.path.getmtime)
        while len(backups) > 10:
            os.remove(backups.pop(0))
            
    except Exception as e:
        logger.error(f"❌ Database backup failed: {e}")

def heal_zombie_bots(conn):
    """
    On startup, structurally repairs any bot that has become mathematical 'zombies'.
    This covers 1. bots with total_invested > 0 but ledger evaluates to 0,
    and 2. bots with current_step > 0 but total_invested is 0.
    """
    try:
        c = conn.cursor()
        c.execute('''
            SELECT t.bot_id, b.pair, t.total_invested, t.avg_entry_price, t.current_step, t.cycle_id 
            FROM trades t JOIN bots b ON t.bot_id = b.id 
        ''')
        trades = c.fetchall()
        for t in trades:
            bot_id, pair, invested, avg_price, step, cycle_id = t
            invested = float(invested or 0)
            avg_price = float(avg_price or 0)
            step = int(step or 0)
            
            # Scenario 1: Ghost step stuck (0 physical investment, but step > 0)
            # This causes the "0/2 limit orders missing" alert
            if step > 0 and invested <= 0.0001:
                c.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, cycle_id = NULL WHERE bot_id = ?", (bot_id,))
                logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Reset stranded ghost step back to 0. Cleared metrics.")
                continue

            # Scenario 3: Phantom Invested Amount (Stuck metrics on a Scanning Bot)
            # This corrects databases where the user manually reverted step=0 but forgot to zero metrics.
            if step == 0 and (invested > 0.001 or avg_price > 0.001):
                c.execute("UPDATE trades SET total_invested = 0, avg_entry_price = 0, target_tp_price = 0, cycle_id = NULL WHERE bot_id = ?", (bot_id,))
                logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Purged phantom ${invested:.2f} invested memory on a SCANNING bot.")
                continue
                
            if invested > 0.0001 and avg_price > 0:
                expected_qty = invested / avg_price
                c.execute('''
                    SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END), 0) -
                           COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0)
                    FROM bot_orders WHERE bot_id = ? AND filled_amount > 0 AND (cycle_id = ? OR cycle_id IS NULL)
                    AND status NOT IN ('reset_cleared', 'auto_closed')
                ''', (bot_id, cycle_id))
                ledger_qty = float(c.fetchone()[0] or 0.0)
                
                # Scenario 2: Physical inventory exists, but ledger evaluates as empty (or wrong)
                if abs(ledger_qty - expected_qty) > 0.001:
                    qty_to_add = expected_qty - ledger_qty
                    cid = f'CQB_{bot_id}_HEAL_{int(time.time()*1000)}'
                    order_type = 'adoption_add' if qty_to_add > 0 else 'adoption_reduce'
                    amount_abs = abs(qty_to_add)
                    c.execute('''
                        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, updated_at, client_order_id, notes, cycle_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, 'Self-healed corrupted ledger limit bug (Boot Phase)', ?)
                    ''', (bot_id, step, order_type, cid, avg_price, amount_abs, amount_abs, int(time.time()), int(time.time()), cid, cycle_id))
                    logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Re-aligned corrupted ledger math. Added {qty_to_add:.4f} units.")
    except Exception as e:
        logger.error(f"Error during zombie bot healing: {e}")

def init_db():
    """Initializes the database schema and performs necessary migrations."""
    # 1. Perform Safety Backup
    backup_database()

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
                manual_close_pct REAL DEFAULT 100.0,
                last_error TEXT,
                last_error_time INTEGER
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

        # [NEW] Migration for last_error columns
        try:
            cursor.execute('SELECT last_error FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bots ADD COLUMN last_error TEXT")
            cursor.execute("ALTER TABLE bots ADD COLUMN last_error_time INTEGER")
            conn.commit()
            logger.info("🛠️ Database Migration: Added last_error columns to bots table.")
        
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

        # Migration for position limit hit flag (v0.9.0)
        # Set when Binance rejects a grid due to max position notional / margin constraints.
        # Cleared automatically on TP reset or when a new fill arrives.
        try:
            cursor.execute('SELECT pos_limit_hit FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bots ADD COLUMN pos_limit_hit INTEGER DEFAULT 0")
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
                filled_amount REAL DEFAULT 0,
                status TEXT DEFAULT 'open',
                created_at INTEGER,
                client_order_id TEXT,
                updated_at INTEGER DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        
        # Migration for filled_amount (v0.6.1)
        try:
            cursor.execute('SELECT filled_amount FROM bot_orders LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE bot_orders ADD COLUMN filled_amount REAL DEFAULT 0')
            conn.commit()
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_pair ON bots(pair)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_status ON bot_orders(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_type ON bot_orders(order_type)')
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

        # --- FUNDAMENTAL FIX: TRACEABILITY LOG ---
        # Reconciliation logs table (v2.1.0 Fundamental Architecture)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reconciliation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                bot_id INTEGER,
                pair TEXT,
                action TEXT,
                details TEXT,
                proof_order_id TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_recon_logs_bot ON reconciliation_logs(bot_id)')

        # FUNDAMENTAL FIX: Clear stale active positions on startup
        # This prevents the UI from showing "Green" (Synced) against old data before the first poll cycle
        cursor.execute('DELETE FROM active_positions')

        heal_zombie_bots(conn)
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
        cursor.execute("INSERT INTO bots (name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Scanning')", (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json))
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

def flag_bot_pos_limit(bot_id: int, state: bool) -> None:
    """Set or clear the position-limit-hit flag for a bot.
    
    When Binance rejects a grid order with any position-cap error (400 Margin
    insufficient, -2027, -2019, etc.) the engine calls this to set state=True.
    The flag is automatically cleared by reset_bot_after_tp on TP hit.
    """
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE bots SET pos_limit_hit = ? WHERE id = ?",
            (1 if state else 0, bot_id)
        )
        conn.commit()
        if state:
            logger.warning(f"🚫 [POS-LIMIT] Bot {bot_id} flagged — exchange position cap reached.")
        else:
            logger.info(f"✅ [POS-LIMIT] Bot {bot_id} position limit flag cleared.")
    except Exception as e:
        logger.error(f"Failed to set pos_limit_hit for bot {bot_id}: {e}")

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
               b.direction, b.is_active,
               COALESCE(t.entry_confirmed, 0) as entry_confirmed,
               COALESCE(t.cycle_id, 1) as cycle_id,
               COALESCE(b.pos_limit_hit, 0) as pos_limit_hit
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
        'is_active': row[11],
        'entry_confirmed': row[12],
        'cycle_id': row[13],
        'pos_limit_hit': bool(row[14]),
    }

    # --- SAFETY GUARD: Log suspicious entry prices but do NOT wipe state ---
    # Previously this wiped bot state when avg_entry < $10,000 for BTC pairs,
    # but this caused infinite re-entry loops when fills set intermediate prices.
    if result['pair'] and 'BTC' in result['pair'] and result['avg_entry_price'] > 0 and result['avg_entry_price'] < 10000:
        logger.warning(f"⚠️ SAFETY NOTICE: Bot {result['name']} has low Entry Price (${result['avg_entry_price']:.2f}). May be mid-fill. NOT wiping state.")
        
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

# --- FUNDAMENTAL FIX: DATA VALIDATION LAYER ---
def validate_trade_data(bot_id, step, invested, avg_price):
    """
    Validates trade data before writing to DB.
    Blocks 'nonsense' data that causes PnL explosions ($9M error).
    """
    # 1. Price Floor: Prevent mathematically corrupted zero-ish prices (e.g. from missing API data)
    if avg_price > 0 and avg_price < 0.0001:
        logger.critical(f"🛡️ VALIDATION BLOCKED: Bot {bot_id} attempted to save impossible avg_price={avg_price}. REJECTING.")
        return False
    
    # 2. Logic Mismatch: Cannot have invested $>0$ with step 0 (or vice versa in some strategies)
    if invested < 0:
        logger.critical(f"🛡️ VALIDATION BLOCKED: Bot {bot_id} has negative invested amount: {invested}")
        return False
        
    return True

def update_martingale_step(bot_id, step, total_invested, avg_price, tp_price):
    """Updates or inserts the trade state for a specific bot (UPSERT logic)."""
    
    # --- FUNDAMENTAL FIX: PRE-WRITE VALIDATION ---
    if not validate_trade_data(bot_id, step, total_invested, avg_price):
        return False

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
                    entry_confirmed = 1
                WHERE bot_id = ? AND (current_step <= ? OR ? = 0)
            """, (step, total_invested, avg_price, tp_price, bot_id, step, step))
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

def calculate_step_from_position(total_invested: float, base_size: float, multiplier: float) -> int:
    """
    Reverse-engineers the Martingale Step based on the cumulative total_invested.
    Since total_invested = Σ(base_size * multiplier^i), it is a geometric series.
    """
    if total_invested <= 0 or base_size <= 0:
        return 0
        
    ratio = total_invested / base_size
    if ratio <= 1:
        return 0
        
    import math
    if multiplier <= 1.0001:
        # Linear sum: total = base * (step + 1)
        step = ratio - 1
    else:
        # Geometric sum: total = base * (1 - multiplier^(step+1)) / (1 - multiplier)
        # Therefore: multiplier^(step+1) = 1 + (total/base)*(multiplier-1)
        inner = 1 + ratio * (multiplier - 1)
        if inner <= 0: return 0
        step = (math.log(inner) / math.log(multiplier)) - 1
        
    return max(0, int(round(step)))

def reset_bot_after_tp(bot_id, exit_price, direction=None, action_label='TP_HIT', notes=''):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT total_invested, current_step, avg_entry_price, name, pair, direction, config FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row: return
        total_invested, current_step, avg_entry_price, bot_name, pair, db_direction, config_str = row
        
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
        
        # 🚀 FUNDAMENTAL FIX: Cross-cycle Carry-over for Orphaned Partial Fills.
        # Before wiping the ledger, check if the bot_orders ledger sum is non-zero.
        # This happens if an entry/grid was partially filled, then cancelled, and never reached TP size.
        cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,))
        old_cycle = int(cursor.fetchone()[0] or 1)
        new_cycle = old_cycle + 1

        # Calculate the mathematical net quantity of the OLD cycle before archiving
        # 🚀 FUNDAMENTAL FIX: Unified True Math for both LONG and SHORT
        # Both directions use `entries - exits` to find the remaining positive unclosed positional magnitude.
        # 🚀 CARRY-LOOP FIX: Explicitly exclude cross-cycle carry rows (client_order_id LIKE '%_CARRY_%')
        # from this calculation. Including them causes a runaway feedback loop where each reset
        # computes old_net_qty from the carry row, inserts a new carry of the same size, and repeats.
        cursor.execute("""
            SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END), 0) -
                   COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0)
            FROM bot_orders WHERE bot_id = ? AND filled_amount > 0 AND (cycle_id = ? OR cycle_id IS NULL)
            AND status NOT IN ('reset_cleared', 'auto_closed')
        """, (bot_id, old_cycle))
        
        # 🚀 FUNDAMENTAL FIX: Clamp negative values to 0 to prevent structural deficits
        old_net_qty = max(0.0, float(cursor.fetchone()[0] or 0.0))

        # 🔑 CYCLE-ID ARCHIVE: Mark ALL bot_orders from the OLD cycle as reset_cleared.
        # This is done BEFORE inserting the carry-over row so the old math is safely hidden.
        cursor.execute("UPDATE bot_orders SET status = 'auto_closed', updated_at = ? WHERE bot_id = ? AND status = 'open'", (int(time.time()), bot_id))
        cursor.execute("UPDATE bot_orders SET status = 'reset_cleared', updated_at = ? WHERE bot_id = ? AND status IN ('filled', 'closed', 'missing', 'cancelled', 'canceled') AND order_type != 'hedge'", (int(time.time()), bot_id))

        # Carry-over Logic: If net_qty is significant, bridge it into the new cycle.
        # 🚀 FUNDAMENTAL FIX: Never carry over ghost mass during a catastrophic system reset.
        excluded_carry_labels = ['RESET_VANISHED_POSITION', 'RESET_STRUCTURAL_GHOST', 'RESET_PHANTOM_ENTRY', 'SYSTEM_WIPE', 'MANUAL_CLOSE', 'STOP_LOSS_EXIT']
        if abs(old_net_qty) > 0.0001 and action_label not in excluded_carry_labels:
            logger.info(f"🌉 [CARRY-OVER] Bot {bot_id}: Carrying over {old_net_qty:.4f} {pair} units into Cycle {new_cycle}.")
            carry_otype = 'adoption_add' if old_net_qty > 0 else 'adoption_reduce'
            carry_cid = f"CQB_{bot_id}_CARRY_{int(time.time() * 1000)}"
            cursor.execute("""
                INSERT INTO bot_orders (
                    bot_id, step, order_type, order_id, price, amount, filled_amount,
                    status, created_at, updated_at, client_order_id, notes, cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reset_cleared', ?, ?, ?, ?, ?)
            """, (
                bot_id, 0, carry_otype, carry_cid, exit_price, abs(old_net_qty), abs(old_net_qty),
                int(time.time()), int(time.time()), carry_cid, 
                f"Cross-cycle carry over of prior partial fills ({old_net_qty:.4f} units)",
                new_cycle
            ))
            # Note: Carry-over row is marked 'reset_cleared' so it doesn't inflate 'total_invested' 
            # in trades table calculation yet, but the monitor.py JOIN will include it in virtual net sum.
            # wait, if it's 'reset_cleared' then monitor.py skips it logic: status NOT IN ('reset_cleared', 'auto_closed')
            # So it MUST be 'filled'.
            cursor.execute("UPDATE bot_orders SET status = 'filled' WHERE client_order_id = ?", (carry_cid,))

        cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = ?, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = ?, cycle_id = ? WHERE bot_id = ?", 
                        (exit_price, int(time.time()), int(time.time()), action_label, new_cycle, bot_id))
            
        cursor.execute("UPDATE bots SET pos_limit_hit = 0 WHERE id = ?", (bot_id,))

        # 🚀 FUNDAMENTAL FIX: Remove the active_positions row for this bot on reset.
        # Without this, the mismatch monitor accumulates stale rows from previous cycles.
        try:
            clear_active_position_for_bot(bot_id, pair)
        except Exception as e_ap:
            logger.warning(f"[ACTIVE-POS] Could not clear active_positions for bot {bot_id}: {e_ap}")
        
        # Check Stop After Cycle
        stop_after_cycle = False
        try:
            if config_str:
                config_data = json.loads(config_str)
                stop_after_cycle = bool(config_data.get('post_exit_stop', False))
        except:
            pass
            
        if stop_after_cycle:
            cursor.execute("UPDATE bots SET status='STOPPED', is_active=0 WHERE id = ?", (bot_id,))
            logger.info(f"🛑 Bot {bot_name} paused due to 'Stop After Cycle' setting.")
            add_notification('warning', f"Bot {bot_name} paused after cycle completion (Stop After Cycle enabled).", bot_id)
        else:
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
    Fixes 'Zombie', 'Ghost', and 'Corrupted' (invalid prices) states.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 0. Fix Corrupted Data (The $9M PnL Bug)
    # Wipe any trade with impossible entry prices OR where total_invested / avg_entry_price * avg_entry_price != total_invested
    cursor.execute("SELECT bot_id, name, avg_entry_price, total_invested FROM trades t JOIN bots b ON t.bot_id = b.id WHERE avg_entry_price > 0 AND total_invested > 0")
    corrupted_candidates = cursor.fetchall()
    
    fixed_count = 0
    for bid, bname, bprice, btotal in corrupted_candidates:
        # Check for impossible entry prices (e.g. practically zero due to API flaws)
        if float(bprice) > 0 and float(bprice) < 0.0001:
            logger.critical(f"☢️ DATA INTEGRITY ALERT: Bot {bname} (ID {bid}) has CORRUPTED entry price ${bprice}. Wiping trade state to prevent PnL explosion.")
            cursor.execute("UPDATE trades SET current_step=0, total_invested=0, avg_entry_price=0, entry_confirmed=0 WHERE bot_id=?", (bid,))
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bid,))
            fixed_count += 1
            continue # Move to next bot after fixing this one

        # Check for float precision corruption: total_invested / avg_entry_price * avg_entry_price should equal total_invested
        # Use a tolerance (rounding) to avoid microscopic float drift wiping valid trades
        if float(btotal) > 0.0 and float(bprice) > 0.0:
            implied_amount = float(btotal) / float(bprice)
            # Round both to 4 decimals to avoid microscopic float drift wiping valid trades
            if round(implied_amount * float(bprice), 4) != round(float(btotal), 4):
                logger.critical(f"☢️ DATA INTEGRITY ALERT: Bot {bname} (ID {bid}) has CORRUPTED total_invested/avg_entry_price relationship (total={btotal}, avg_price={bprice}). Wiping trade state to prevent PnL explosion.")
                cursor.execute("UPDATE trades SET total_invested=0, avg_entry_price=0, current_step=0, entry_confirmed=0 WHERE bot_id=?", (bid,))
                cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bid,))
                fixed_count += 1
                
    cursor.execute("SELECT b.id, b.name, b.status, t.total_invested, t.entry_order_id FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1")
    rows = cursor.fetchall()
    
    # Also clear stale trade data for inactive bots to avoid confusing the reconciler
    cursor.execute("""
        UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0, entry_order_id=NULL, tp_order_id=NULL
        WHERE bot_id IN (SELECT id FROM bots WHERE is_active=0)
          AND (total_invested > 0 OR current_step > 0 OR entry_order_id IS NOT NULL)
    """)
    inactive_cleared = cursor.rowcount
    if inactive_cleared > 0:
        logger.warning(f"⚠️ [INTEGRITY] Cleared stale trade data for {inactive_cleared} inactive bots.")
        fixed_count += inactive_cleared
        
    for row in rows:
        bot_id, name, status, invested, entry_order_id = row
        invested = invested or 0
        
        # Case 1: Zombie State (Active Status but No Money)
        # Status "IN TRADE" (or similar) but invested is 0 AND NO pending entry order exists
        if status and status.upper() in ['IN TRADE', 'TRADING', '🔴 IN TRADE'] and invested <= 0:
            if entry_order_id:
                pass # Legitimately waiting for an entry fill, do NOT reset.
            else:
                logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is '{status}' but has $0 invested and no entry_order_id. Resetting to 'Scanning'.")
                cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
                fixed_count += 1
            
        # Case 2: Ghost State (Scanning Status but Money trapped)
        # Status "Scanning" but invested > 0
        # FLAG-ONLY: Don't auto-promote — let reconciler decide with evidence.
        # Auto-promoting fights ghost-bust and causes cascade loops.
        elif status and (status.upper() == 'SCANNING' or status.upper() == '🟢 SCANNING') and invested > 1.0: # Ignore dust < $1
            logger.warning(f"⚠️ [FLAG-ONLY] Bot {name} (ID {bot_id}) is '{status}' but has ${invested:.2f} invested. NOT auto-promoting — reconciler will handle.")

        # Case 3: Stuck Stopped (Active bot with 'Stopped' status)
        elif status and status.upper() == '⚪ STOPPED' and invested > 1.0:
            logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is 'Stopped' but has ${invested:.2f} invested. Resetting trade to 0.")
            cursor.execute("UPDATE trades SET current_step=0, total_invested=0, avg_entry_price=0, entry_confirmed=0 WHERE bot_id=?", (bot_id,))
            fixed_count += 1
            
        # Case 4: REMOVED (Fundamental VPS handle via evidence-based reconciler)
        # is_active=1 but status is still 'Stopped'/'STOPPED' from before toggle fix
        elif status and status.upper() == 'STOPPED':
            if invested > 1.0:
                logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is active but status='Stopped' with ${invested:.2f} invested. Updating to 'IN TRADE'.")
                cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
            else:
                logger.warning(f"🔧 Integrity Fix: Bot {name} (ID {bot_id}) is active but status='Stopped'. Updating to 'Scanning'.")
                cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
            fixed_count += 1

    # Also fix any legacy "Waiting for Signal" rows to "Scanning" standard
    cursor.execute("UPDATE bots SET status='Scanning' WHERE status='Waiting for Signal'")
    
    # Fix any bots with NULL status
    cursor.execute("UPDATE bots SET status='Scanning' WHERE status IS NULL OR status = ''")

    # Fix any bots with 'futures' market_type in config → canonical 'future'
    cursor.execute("SELECT id, config FROM bots WHERE config LIKE '%\"market_type\": \"futures\"%' OR config LIKE '%\"market_type\":\"futures\"%'")
    for row in cursor.fetchall():
        bot_id_fix, config_json = row
        if config_json:
            try:
                cfg = json.loads(config_json)
                if cfg.get('market_type') == 'futures':
                    cfg['market_type'] = 'future'
                    cursor.execute("UPDATE bots SET config = ? WHERE id = ?", (json.dumps(cfg), bot_id_fix))
                    logger.warning(f"🔧 Integrity Fix: Bot ID {bot_id_fix} config market_type 'futures' → 'future'.")
                    fixed_count += 1
            except: pass
    
    # Removed the dangerous "Clear stale order IDs for zero-invested rows" block,
    # because our new Surgical Lock pattern relies on setting invested=0 and entry_order_id.

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

def upsert_active_position_for_bot(bot_id: int, pair: str, direction: str, avg_fill_price: float) -> None:
    """
    Write (or update) the active_positions row for this bot using its virtual ledger.

    ROOT-CAUSE FIX for multi-bot One-Way mode: previously active_positions was only written
    by import_position_from_exchange (REALITY-AUTO-MAP path), which only fires for the one bot
    whose proof-order the reconciler can match. Every other same-direction bot on the pair was
    left without an active_positions row, so the mismatch monitor fired every cycle.

    Now called from ws_event_handlers after every fill so every bot with a position
    always has its own active_positions row immediately.

    NOTE: does NOT call conn.close() — get_connection() is a thread-local singleton;
    closing it would break all subsequent DB calls on this thread.
    """
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT total_invested, avg_entry_price FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        if not row or float(row[0] or 0) <= 0:
            return
        total_invested = float(row[0])
        avg_price = float(row[1]) if row[1] and float(row[1]) > 0 else avg_fill_price
        if avg_price <= 0:
            return
        virtual_qty = total_invested / avg_price

        from engine.exchange_interface import normalize_symbol
        clean_pair = normalize_symbol(pair)
        side = direction.upper()

        conn.execute(
            """INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(bot_id, pair, side)
               DO UPDATE SET size=excluded.size, entry_price=excluded.entry_price,
                             last_checked=excluded.last_checked, last_updated=excluded.last_updated""",
            (bot_id, clean_pair, side, virtual_qty, avg_price, int(time.time()))
        )
        conn.commit()
        logger.debug(f"[ACTIVE-POS] Bot {bot_id} ({clean_pair} {side}): upserted qty={virtual_qty:.6f} @ {avg_price:.4f}")
    except Exception as e:
        logger.error(f"[ACTIVE-POS] Failed to upsert active_positions for bot {bot_id}: {e}")


def clear_active_position_for_bot(bot_id: int, pair: str = None) -> None:
    """
    Remove the active_positions row(s) for this bot when it resets after TP/close.
    Called from reset_bot_after_tp so the entry disappears when the position is gone.

    NOTE: does NOT call conn.close() — get_connection() is a thread-local singleton.
    """
    try:
        conn = get_connection()
        if pair:
            from engine.exchange_interface import normalize_symbol
            clean_pair = normalize_symbol(pair)
            conn.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
        else:
            conn.execute("DELETE FROM active_positions WHERE bot_id = ?", (bot_id,))
        conn.commit()
        logger.debug(f"[ACTIVE-POS] Bot {bot_id}: cleared active_positions for pair={pair or 'all'}")
    except Exception as e:
        logger.error(f"[ACTIVE-POS] Failed to clear active_positions for bot {bot_id}: {e}")


def import_position_from_exchange(bot_id: int, pair: str, position_size: float, entry_price: float, direction: str) -> Tuple[bool, str]:

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT direction, martingale_multiplier, base_size, config FROM bots WHERE id = ?", (bot_id,))
    params = cursor.fetchone()
    if not params: return False, "Bot not found in database."
    bot_direction, multiplier, base_size, config_json = params

    if direction.upper() != bot_direction.upper(): 
        return False, f"Cannot adopt {direction.upper()} position into a {bot_direction.upper()} bot. Please change bot direction first."
    
    # --- UNCLAIMED REMAINDER ADOPTION ---
    # Architecture principle: each bot tracks only what its own orders contributed.
    # import_position_from_exchange is for *rogue* positions (nothing claims them).
    # If other active bots already have total_invested > 0 for this pair+direction,
    # they are already tracking their share. This bot should only claim what's left.
    #
    # Sum up what ALL OTHER same-pair + same-direction active bots already track.
    cursor.execute(
        "SELECT COALESCE(SUM(t.total_invested), 0.0) "
        "FROM bots b JOIN trades t ON b.id = t.bot_id "
        "WHERE b.pair = ? AND UPPER(b.direction) = UPPER(?) AND b.is_active = 1 AND t.total_invested > 0 AND b.id != ?",
        (pair, direction, bot_id)
    )
    already_claimed_usd = float(cursor.fetchone()[0])

    full_notional = abs(float(position_size)) * float(entry_price)
    unclaimed_notional = full_notional - already_claimed_usd

    if unclaimed_notional <= 5.0:
        # The physical position is already fully accounted for by existing bots.
        # Adopting again would duplicate the virtual net — block it.
        conn.close()
        msg = (
            f"Position already fully tracked by existing bots "
            f"(Sibling claim: ${already_claimed_usd:.2f} / Physical: ${full_notional:.2f}). "
            f"Adoption blocked to prevent duplicate virtual net."
        )
        logger.warning(f"⛔ [ADOPT-BLOCKED] Bot {bot_id} ({pair} {direction}): {msg}")
        return False, msg

    total_invested = unclaimed_notional
    adopted_size = unclaimed_notional / float(entry_price) if float(entry_price) > 0 else abs(float(position_size))

    if already_claimed_usd > 0:
        logger.warning(
            f"⚖️ [PARTIAL-ADOPT] Bot {bot_id} ({pair} {direction}): "
            f"Sibling bots already claim ${already_claimed_usd:.2f}. "
            f"Adopting unclaimed remainder: ${total_invested:.2f} of ${full_notional:.2f}."
        )
    else:
        logger.info(
            f"✅ [FULL-ADOPT] Bot {bot_id} ({pair} {direction}): "
            f"No other bots claim this position. Adopting full notional: ${total_invested:.2f}."
        )


    # FUNDAMENTAL FIX: Calculate and save target_tp_price on adoption
    import json
    from engine.runner import BotRunner # Mildly circular, but necessary for strategy access
    from engine.strategies.martingale_strategy import MartingaleStrategy
    
    runner_instance = BotRunner.get_instance()
    if runner_instance:
        bot_params = json.loads(config_json) if config_json else {}
        strategy = runner_instance.get_strategy(bot_id, bot_params)
        bot_status = {'avg_entry_price': float(entry_price), 'total_invested': total_invested, 'current_step': 0}
        tp_price = strategy.calculate_take_profit_price(bot_status=bot_status, current_price=float(entry_price))
    else:
        # Fallback if runner isn't available (e.g., standalone script)
        tp_price = float(entry_price) * 1.015 if direction.upper() == 'LONG' else float(entry_price) * 0.985

    # 🚀 STEP RECOVERY: Instead of blindly forcing Step 1, dynamically recover the true step.
    # Because order history might be zeroed or belong to a past cycle during a crash,
    # the most reliable way to recover the step is through raw mathematical derivation.
    calculated_step = 1
    try:
        # Use the config params to mathematically determine how many steps this position size represents.
        db_base_size = float(base_size) if base_size else 10.0
        db_mult = float(multiplier) if multiplier else 1.05
        
        if config_json:
            cfg = json.loads(config_json)
            db_base_size = float(cfg.get('base_order_size', db_base_size))
            db_mult = float(cfg.get('martingale_multiplier', db_mult))
        
        if db_base_size > 0 and total_invested > 0:
            if total_invested <= db_base_size * 1.1:
                calculated_step = 1
            elif db_mult > 1:
                # Math: Total invested = Base * (1 - r^step) / (1 - r) where r = multiplier
                # Simulation is more robust to rounding/min-lot-size drift.
                simulated_cumulative = 0.0
                current_layer_size = db_base_size
                for s in range(1, 51): # Cap at 50 to prevent infinite loops
                    simulated_cumulative += current_layer_size
                    if simulated_cumulative >= total_invested * 0.98: # 2% tolerance for fees/rounding
                        calculated_step = s
                        break
                    current_layer_size *= db_mult
            else:
                # Linear progression (mult = 1)
                calculated_step = max(1, int(round(total_invested / db_base_size)))
        
        logger.info(f"🔄 [STEP-RECOVERY] Bot {bot_id} mathematically derived Step {calculated_step} from ${total_invested:.2f} invested.")
    except Exception as e:
        logger.warning(f"Mathematical step recovery failed for bot {bot_id}: {e}")
    try:
        # Check if record exists (UPSERT logic)
        cursor.execute("SELECT current_step FROM trades WHERE bot_id = ?", (bot_id,))
        exists = cursor.fetchone()
        
        if exists:
            existing_step = exists[0] or 0
            # If bot already has a step, preserve it — don't overwrite
            use_step = existing_step if existing_step > 0 else calculated_step
            # Do NOT overwrite basket_start_time with 0, as it exposes the bot to past historical orders.
            # Instead, set it to the current time to enforce a forward-only sync paradigm.
            cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=?, target_tp_price=?, basket_start_time=?, entry_confirmed=1 WHERE bot_id=?", 
                           (use_step, total_invested, float(entry_price), tp_price, int(time.time()), bot_id))
            logger.debug(f"import_position_from_exchange: UPDATED bot {bot_id}")
        else:
            # INSERT new record
            # Setting basket_start_time to now ensures that reconciler only sees newly generated orders going forward.
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price, basket_start_time, entry_confirmed)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (bot_id, calculated_step, total_invested, float(entry_price), tp_price, int(time.time())))
            logger.debug(f"import_position_from_exchange: INSERTED bot {bot_id}")
            
        # --- FUNDAMENTAL ARCHITECTURE FIX: EVIDENCE-PROOF ADOPTION ---
        # The Reconciler demands cryptographic proof of legal ownership.
        # We must generate an iron-clad fill record, or it will be wiped as a "Ghost".
        # 📝 LEDGER FIX: Explicitly set filled_amount to match amount.
        evidence_cid = f"CQB_{bot_id}_ADOPT_{int(time.time())}"
        cursor.execute("""
            INSERT INTO bot_orders (
                bot_id, step, order_type, order_id, price, amount, filled_amount,
                status, created_at, updated_at, client_order_id, notes
            ) VALUES (?, ?, 'adoption_add', ?, ?, ?, ?, 'filled', ?, ?, ?, ?)
        """, (
            bot_id, 
            calculated_step, 
            evidence_cid,            # Use CID as Exchange ID to guarantee uniqueness 
            float(entry_price), 
            adopted_size,            # Proportional — NOT the full exchange position_size
            adopted_size,            # filled_amount MUST match amount for True Math
            int(time.time()), 
            int(time.time()), 
            evidence_cid, 
            "Native Position Adoption (Evidence)"
        ))
        logger.debug(f"import_position_from_exchange: GENERATED EVIDENCE for bot {bot_id} ({evidence_cid})")
        
        conn.commit()
        return True, "Success"
    except Exception as e:
        conn.rollback()
        logger.error(f"import_position_from_exchange failed for bot {bot_id}: {e}")
        return False, str(e)

def get_all_bots():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT b.id, b.name, b.pair, b.is_active, b.strategy_type, COALESCE(t.total_invested, 0), COALESCE(t.current_step, 0), b.last_error, b.last_error_time FROM bots b LEFT JOIN trades t ON b.id = t.bot_id")
    bots = cursor.fetchall()
    logger.debug(f"[GET_ALL_BOTS] Query returned {len(bots)} bots from DB.")
    return bots

def toggle_bot_active(bot_id, new_status):
    conn = get_connection()
    cursor = conn.cursor()
    if new_status:
        cursor.execute("UPDATE bots SET is_active = 1, status = 'Scanning' WHERE id = ?", (bot_id,))
    else:
        cursor.execute("UPDATE bots SET is_active = 0 WHERE id = ?", (bot_id,))
    conn.commit()

def update_bot_error(bot_id: int, error_msg: str):
    """Updates the last_error field for a bot. Set error_msg to None to clear."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE bots SET last_error = ?, last_error_time = ? WHERE id = ?",
            (error_msg, int(time.time()) if error_msg else None, bot_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update bot error for {bot_id}: {e}")

from typing import List, Dict
def update_active_positions(positions: List[Dict]):
    """
    Populates the active_positions table for UI visibility.
    Accepts CCXT-formatted position objects.
    Uses explicit transaction logic to prevent race conditions.
    """
    conn = get_connection()
    # Enforce isolation
    conn.isolation_level = None 
    cursor = conn.cursor()
    
    timestamp = int(time.time())
    
    try:
        cursor.execute("BEGIN TRANSACTION")
        cursor.execute("DELETE FROM active_positions")
        
        # Fetch and aggregate positions to prevent Binance fragmented duplicate flaws
        agg_positions = {}
        for p in positions:
            pair = p.get('symbol', 'Unknown')
            raw_size = p.get('contracts', 0) or p.get('info', {}).get('positionAmt', 0)
            size = float(raw_size)
            price = float(p.get('entryPrice', 0) or 0)
            side = p.get('side', 'long').upper() # 'long' or 'short'
            
            if abs(size) > 0:
                key = (pair, side)
                if key not in agg_positions:
                    agg_positions[key] = {'size': abs(size), 'value': abs(size) * price}
                else:
                    agg_positions[key]['size'] += abs(size)
                    agg_positions[key]['value'] += (abs(size) * price)

        # Insert cleanly aggregated positions
        for (pair, side), data in agg_positions.items():
            avg_price = data['value'] / data['size'] if data['size'] > 0 else 0
            owner_id = get_active_bot_id_by_symbol_direction(pair, side) or 0
            cursor.execute("""
                INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (owner_id, pair, side, data['size'], avg_price, timestamp))
            
        cursor.execute("COMMIT")
    except Exception as e:
        cursor.execute("ROLLBACK")
        logger.error(f"Failed to update active_positions table: {e}")
    finally:
        conn.close()

def delete_bot(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # SAFETY CHECK 1: Check for Active Trade
        cursor.execute("SELECT total_invested FROM trades WHERE bot_id = ?", (bot_id,))
        trade = cursor.fetchone()
        if trade and trade[0] > 0:
            logger.warning(f"⚠️ BLOCKED DELETION: Bot {bot_id} has active trade (${trade[0]}). Close position first.")
            return False

        # SAFETY CHECK 2: Check for Open Orders
        cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status='open'", (bot_id,))
        open_orders = cursor.fetchone()[0]
        if open_orders > 0:
            logger.warning(f"⚠️ BLOCKED DELETION: Bot {bot_id} has {open_orders} open orders. Cancel them first.")
            return False

        # Proceed with deletion if safe
        cursor.execute('DELETE FROM trade_history WHERE bot_id = ?', (bot_id,))
        cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
        cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error deleting bot {bot_id}: {e}")
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
        cursor.execute("SELECT id, bot_id, order_type, client_order_id FROM bot_orders WHERE status IN ('open', 'new') AND created_at < ?", (threshold_time,))
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

# Module-level counter for consecutive empty snapshots (prevents permanent stale data)
_EMPTY_SNAP_COUNTER = 0
_EMPTY_SNAP_THRESHOLD = 3  # Allow clearing after 3 consecutive empty snapshots

def update_active_positions_snapshot(positions: list):
    """
    Updates the active_positions table with the latest snapshot from the exchange.
    This provides the UI with a real-time view of 'Physical Reality'.
    """
    global _EMPTY_SNAP_COUNTER
    conn = None
    try:
        conn = get_connection()
        
        # SAFETY LATCH v2: Consecutive-miss counter
        # Protects against transient API failures wiping real data,
        # but allows clearing after sustained empty responses (position genuinely closed).
        if not positions or len(positions) == 0:
             current_count = conn.execute("SELECT COUNT(*) FROM active_positions").fetchone()[0]
             if current_count > 0:
                 _EMPTY_SNAP_COUNTER += 1
                 if _EMPTY_SNAP_COUNTER < _EMPTY_SNAP_THRESHOLD:
                     logger.warning(f"⚠️ [SAFETY-LATCH] Empty snapshot {_EMPTY_SNAP_COUNTER}/{_EMPTY_SNAP_THRESHOLD}. Keeping {current_count} existing positions.")
                     return
                 else:
                     logger.info(f"✅ [SAFETY-LATCH] {_EMPTY_SNAP_COUNTER} consecutive empty snapshots. Clearing {current_count} stale positions.")
                     _EMPTY_SNAP_COUNTER = 0
        else:
            _EMPTY_SNAP_COUNTER = 0  # Reset on non-empty snapshot
        
        # Extract purely unowned orphans from the snapshot.
        # We do NOT wipe `active_positions`. Individual bots manage their own rows natively.
        
        # Determine all raw physical positions
        current_orphans = []
        for p in positions:
            raw_symbol = p.get('symbol', 'UNKNOWN')
            from engine.exchange_interface import normalize_symbol
            symbol = normalize_symbol(raw_symbol)

            amount = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            entry_price = float(p.get('entryPrice', 0))

            p_side = p.get('side', '').lower()
            if p_side == 'short':
                side = 'SHORT'
            elif p_side == 'long':
                side = 'LONG'
            else:
                side = 'LONG' if amount > 0 else 'SHORT'

            if amount == 0:
                continue  

            # If NO bot owns this pair/side, it is an orphan.
            if get_active_bot_id_by_symbol_direction(symbol, side) is None:
                current_orphans.append((symbol, side, abs(amount), entry_price))

        # We first purge all existing orphans (bot_id = 0) so we can insert the fresh ones
        conn.execute("DELETE FROM active_positions WHERE bot_id = 0")
        
        for orphan in current_orphans:
            symbol, side, size, entry_price = orphan
            logger.warning(f"⚠️ [REALITY-ORPHAN] Physical {side} {symbol} has no owner. Saved with bot_id=0 for UI.")
            conn.execute("""
                INSERT OR REPLACE INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                VALUES (0, ?, ?, ?, ?, ?)
            """, (symbol, side, size, entry_price, int(time.time())))
        
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


def accumulate_trade_fill(bot_id: int, added_invested: float, added_qty: float, avg_price: float, new_step, tp_price, is_entry: bool = False, force_step: bool = False):
    """
    Atomically accumulates a fill into the trade state using SQL math.
    new_step may be None (from partial fills) — in that case step is not changed.
    """
    # Guard: None step means "don't change step" — map to a sentinel handled in SQL
    step_is_none = new_step is None
    safe_step = 0 if step_is_none else int(new_step)
    safe_tp = float(tp_price) if tp_price is not None else None
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Check if record exists
        cursor.execute("SELECT total_invested FROM trades WHERE bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        
        if row:
            # ATOMIC UPDATE:
            # 1. total_invested = current + added
            # 2. avg_entry_price = weighted average
            # 3. current_step = updated if force_step=True OR if new_step > current
            # 4. target_tp_price = only updated if tp_price is not None
            if safe_tp is not None:
                cursor.execute("""
                    UPDATE trades
                    SET total_invested = total_invested + ?,
                        avg_entry_price = CASE
                            WHEN total_invested = 0 THEN ?
                            WHEN avg_entry_price > 0 AND ? > 0
                            THEN (total_invested + ?) / ((total_invested / avg_entry_price) + (? / ?))
                            ELSE ?
                        END,
                        current_step = CASE
                            WHEN ? THEN ?
                            WHEN ? THEN ?
                            WHEN ? AND ? > current_step THEN ?
                            ELSE current_step
                        END,
                        target_tp_price = ?,
                        entry_confirmed = 1
                    WHERE bot_id = ?
                """, (
                    added_invested,
                    avg_price,
                    avg_price,
                    added_invested, added_invested, avg_price,
                    avg_price,
                    force_step, safe_step,           # ROOT CAUSE FIX: Reconciler can force alignment
                    is_entry and not step_is_none, safe_step,
                    not step_is_none, safe_step, safe_step,
                    safe_tp,
                    bot_id
                ))
            else:
                # tp_price is None (partial fill) — skip tp update entirely
                cursor.execute("""
                    UPDATE trades
                    SET total_invested = total_invested + ?,
                        avg_entry_price = CASE
                            WHEN total_invested = 0 THEN ?
                            WHEN avg_entry_price > 0 AND ? > 0
                            THEN (total_invested + ?) / ((total_invested / avg_entry_price) + (? / ?))
                            ELSE ?
                        END,
                        current_step = CASE
                            WHEN ? THEN ?
                            WHEN ? AND ? > current_step THEN ?
                            ELSE current_step
                        END,
                        entry_confirmed = 1
                    WHERE bot_id = ?
                """, (
                    added_invested,
                    avg_price,
                    avg_price,
                    added_invested, added_invested, avg_price,
                    avg_price,
                    force_step, safe_step,           # ROOT CAUSE FIX: Reconciler can force alignment
                    not step_is_none, safe_step, safe_step,
                    bot_id
                ))
        else:
            # FIRST ENTRY: Insert new record.
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price, entry_confirmed, basket_start_time, cycle_id)
                VALUES (?, ?, ?, ?, ?, 1, ?,
                    COALESCE((SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id=?), 0) + 1
                )
            """, (bot_id, safe_step, added_invested, avg_price, safe_tp, int(time.time()), bot_id))


            
        # Fix 2: Sync bots.status → 'IN TRADE' immediately on fill
        # The WebSocket handler calls this; bots.status must match trades.total_invested.
        cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
        conn.commit()
        logger.info(f"✅ [ATOMIC-accumulate] Bot {bot_id} trade updated: +${added_invested:.2f} (new step: {new_step}, status→IN TRADE)")
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ [ATOMIC-FAIL] Failed to accumulate fill for bot {bot_id}: {e}")
        raise
    finally:
        conn.close()

def get_active_bot_id_by_symbol_direction(symbol: str, direction: str) -> Optional[int]:
    """
    Look up the bot ID that currently 'owns' a physical footprint on the exchange.

    Primary proof  : A FILLED bot_order with client_order_id='CQB_{bot_id}_...' for this
                     symbol in the bot's current cycle. This is the ONLY reliable signal —
                     a physical position exists because our system created it via a known order.
    Fallback (weak): If no order proof found, match on symbol + direction for a bot that is
                     actively IN TRADE (total_invested > 0). Direction alone is NOT sufficient
                     because two bots on the same pair can have opposite directions.

    A bot can NEVER claim a position it did not create. "Same pair, same direction" is not proof.
    """
    from engine.exchange_interface import normalize_symbol
    norm_symbol = normalize_symbol(symbol).upper()
    norm_direction = direction.upper()

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # --- PRIMARY: Order-ID Proof ---
        # Find any bot_order with a CQB client_order_id whose bot is active on this symbol.
        # The prefix CQB_{bot_id}_ is deterministic and unique per bot — this is ground truth.
        cursor.execute("""
            SELECT DISTINCT bo.bot_id
            FROM bot_orders bo
            JOIN bots b ON bo.bot_id = b.id
            JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
              AND t.total_invested > 0
              AND bo.status IN ('filled', 'open', 'new', 'reset_cleared')
              AND bo.filled_amount > 0
              AND bo.client_order_id LIKE 'CQB_%'
        """)
        candidate_bots = [r[0] for r in cursor.fetchall()]

        for bid in candidate_bots:
            cursor.execute("SELECT pair, direction FROM bots WHERE id = ?", (bid,))
            row = cursor.fetchone()
            if not row:
                continue
            bpair, bdir = row
            if normalize_symbol(bpair).upper() == norm_symbol and bdir.strip().upper() == norm_direction:
                # Confirmed: this bot placed a real order for this symbol AND matches the position direction.
                return bid

        # --- FALLBACK: Direction + In-Trade check ---
        # Only accept if the bot is genuinely IN TRADE (has invested capital).
        # This catches cases where WS filled the entry but bot_orders row has no CQB prefix.
        cursor.execute("""
            SELECT b.id, b.pair, b.direction, t.total_invested
            FROM bots b
            JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
              AND t.total_invested > 0
        """)
        for bid, bpair, bdir, invested in cursor.fetchall():
            if (normalize_symbol(bpair).upper() == norm_symbol
                    and bdir.strip().upper() == norm_direction):
                return bid

        return None
    except Exception as e:
        logger.error(f"Error in get_active_bot_id_by_symbol_direction: {e}")
        return None
    # NOTE: No conn.close() — get_connection() returns a thread-local persistent connection.


def recompute_invested_from_orders(bot_id: int) -> tuple:
    """
    Derive (total_invested, avg_entry_price, current_step) from confirmed filled
    bot_orders for the bot's current cycle.

    This is the ORDER-ID-ANCHORED ground truth.  The trades table is a cache;
    this function always reads the underlying confirmed fills directly.

    Two-pass approach:
    Pass 1 — Count regular entry/grid fills (price > 0, no CARRY rows).
              Normal case: bot entered fresh this cycle.

    Pass 2 — If Pass 1 returns 0 AND the cycle has a CARRY row (qty > 0):
              The bot completed a TP and carried residual qty into this cycle.
              CARRY rows have price=0 (bookkeeping only, not an exchange fill).
              Use CARRY qty + active_positions avg_price to reconstruct invested.

    Returns (0.0, 0.0, 0) if the bot truly has no confirmed position this cycle.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # Resolve current cycle_id from trades table
        row = cursor.execute(
            "SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if not row:
            return 0.0, 0.0, 0
        cycle_id = row[0]

        cursor.execute("""
            SELECT
                COALESCE(SUM(bo.filled_amount * bo.price), 0.0) AS total_cost,
                COALESCE(SUM(bo.filled_amount),             0.0) AS total_qty,
                COALESCE(MAX(bo.step),                      0)   AS max_step
            FROM bot_orders bo
            WHERE bo.bot_id  = ?
              AND bo.cycle_id = ?
              AND bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption')
              AND bo.filled_amount > 0
              AND bo.price > 0
              AND bo.client_order_id LIKE 'CQB_%'
              AND bo.client_order_id NOT LIKE '%_CARRY_%'
              AND bo.status NOT IN ('open', 'new', 'placing', 'failed', 'auto_closed', 'reset_cleared')
              -- ↑ Count filled_amount from ALL terminal statuses (filled, cancelled, closed).
              -- Cancelled orders with filled_amount > 0 are real partial fills from the exchange.
              -- We exclude only orders still open/pending or explicitly system-cleared.
        """, (bot_id, cycle_id))
        r = cursor.fetchone()
        total_cost = float(r[0] or 0.0)
        total_qty  = float(r[1] or 0.0)
        max_step   = int(r[2] or 0)

        if total_qty > 1e-8:
            avg_price = total_cost / total_qty
            return total_cost, avg_price, max_step

        # ------------------------------------------------------------------
        # PASS 2: CARRY-only cycle (bot just reset from a TP, no new fills yet)
        # CARRY rows have price=0 — they record residual qty carried forward,
        # not a new exchange execution. Use active_positions for the avg price.
        # ------------------------------------------------------------------
        carry_row = cursor.execute("""
            SELECT COALESCE(SUM(filled_amount), 0.0)
            FROM bot_orders
            WHERE bot_id  = ?
              AND cycle_id = ?
              AND client_order_id LIKE 'CQB_%'
              AND client_order_id LIKE '%_CARRY_%'
              AND filled_amount > 0
              AND status NOT IN ('open', 'new', 'placing', 'failed')
        """, (bot_id, cycle_id)).fetchone()
        carry_qty = float(carry_row[0] or 0.0)

        if carry_qty <= 1e-8:
            return 0.0, 0.0, 0  # Truly no position this cycle

        # Look up active_positions snapshot for entry_price
        bot_row = cursor.execute(
            "SELECT pair, direction FROM bots WHERE id = ?", (bot_id,)
        ).fetchone()
        if not bot_row:
            return 0.0, 0.0, 0

        pair, direction = bot_row
        norm_pair = pair.split(':')[0].replace('/', '')
        snap_side  = 'LONG' if str(direction).upper() == 'LONG' else 'SHORT'
        snap_row = cursor.execute(
            "SELECT entry_price FROM active_positions WHERE pair=? AND side=?",
            (norm_pair, snap_side)
        ).fetchone()

        if snap_row and float(snap_row[0] or 0) > 0:
            carry_avg_price = float(snap_row[0])
            carry_cost = carry_qty * carry_avg_price
            logger.info(
                f"[RECOMPUTE-CARRY] Bot {bot_id} cycle {cycle_id}: "
                f"CARRY qty={carry_qty:.8f} @ entry_price={carry_avg_price:.4f} "
                f"(from active_positions). total_invested={carry_cost:.4f}"
            )
            return carry_cost, carry_avg_price, 1  # step=1 (carry-forward)

        logger.warning(
            f"[RECOMPUTE-CARRY] Bot {bot_id}: CARRY qty={carry_qty:.8f} but "
            f"no active_positions snapshot found. Cannot price position."
        )
        return 0.0, 0.0, 0

    except Exception as e:
        logger.error(f"Error in recompute_invested_from_orders (bot {bot_id}): {e}")
        return 0.0, 0.0, 0


def sync_trades_from_orders(bot_id: int) -> bool:
    """
    Compare trades.total_invested against the order-ID-anchored ground truth.

    Comparison is done in QUANTITY space (not dollars) to avoid price noise:
        recomputed_qty  = SUM(filled_amount) from confirmed order IDs
        cached_qty      = trades.total_invested / trades.avg_entry_price

    If |recomputed_qty - cached_qty| > 1e-6 (float epsilon for quantity),
    the trades row is updated to match the recomputed values.

    Returns True if a correction was written, False if already in sync.

    Safe to call frequently — it is a no-op when the bot is healthy.
    Only writes to the DB when a real discrepancy is detected.
    """
    QTY_EPSILON = 1e-6  # float addition rounding tolerance in units (not dollars)

    try:
        recomputed_cost, recomputed_avg, recomputed_step = recompute_invested_from_orders(bot_id)

        conn = get_connection()
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        if not row:
            return False

        cached_invested, cached_avg, cached_step = float(row[0] or 0), float(row[1] or 0), int(row[2] or 0)

        # Derive quantity from each source
        recomputed_qty = (recomputed_cost / recomputed_avg) if recomputed_avg > 0 else 0.0
        cached_qty     = (cached_invested / cached_avg)     if cached_avg > 0     else 0.0

        delta_qty = abs(recomputed_qty - cached_qty)

        if delta_qty <= QTY_EPSILON:
            return False  # Already in sync — no write needed

        # Discrepancy detected — recomputed fills are authoritative
        logger.warning(
            f"🔧 [LEDGER-SYNC] Bot {bot_id}: qty drift detected. "
            f"Cached={cached_qty:.8f} vs Confirmed={recomputed_qty:.8f} (Δ={delta_qty:.8f}). "
            f"Correcting trades row from order fills."
        )
        cursor.execute("""
            UPDATE trades
            SET total_invested  = ?,
                avg_entry_price = ?,
                current_step    = ?,
                entry_confirmed = CASE WHEN ? > 0 THEN 1 ELSE entry_confirmed END
            WHERE bot_id = ?
        """, (
            round(recomputed_cost, 8),
            round(recomputed_avg, 8),
            max(recomputed_step, cached_step),   # never go backwards on step
            recomputed_cost,
            bot_id
        ))
        conn.commit()
        return True

    except Exception as e:
        logger.error(f"Error in sync_trades_from_orders (bot {bot_id}): {e}")
        return False


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
            cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = ?, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = 'RECONCILE' WHERE bot_id = ?", (current_price, int(time.time()), int(time.time()), bot_id))
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

def log_reconciliation(bot_id, pair, action, details, proof_order_id=None):
    """Logs a structural reconciliation decision for auditing."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO reconciliation_logs (timestamp, bot_id, pair, action, details, proof_order_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (int(time.time()), bot_id, pair, action, details, proof_order_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to log reconciliation: {e}")

def get_recent_reconciliations(limit=10):
    """Fetches recent reconciliation decisions."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rl.timestamp, b.name, rl.pair, rl.action, rl.details, rl.proof_order_id
            FROM reconciliation_logs rl
            JOIN bots b ON rl.bot_id = b.id
            ORDER BY rl.timestamp DESC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch reconciliation logs: {e}")
        return []

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

# NOTE: import_position_from_exchange is defined earlier (L765) — correct version with full schema.
# NOTE: update_bot_display_status is defined earlier (L728) — correct version with error handling.
        
def update_order_status(order_id, status, bot_id=None, filled_qty=None):
    """
    Updates the status of an order in bot_orders.
    🚀 EVIDENCE PROTECTION: Only updates filled_amount if the new qty is >= existing.
    Prevents late exchange reports (0 quantity remaining) from erasing proven fill history.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Current time as timestamp
        now_ts = int(time.time())

        if filled_qty is not None:
            # Conditional update: only update filled_amount if it increases or matches (never downgrade evidence)
            sql = """
                UPDATE bot_orders 
                SET status = ?, 
                    filled_amount = CASE 
                        WHEN ? >= COALESCE(filled_amount, 0) THEN ? 
                        ELSE filled_amount 
                    END,
                    updated_at = ? 
                WHERE order_id = ?
            """
            params = [status, float(filled_qty), float(filled_qty), now_ts, str(order_id)]
        else:
            sql = "UPDATE bot_orders SET status = ?, updated_at = ? WHERE order_id = ?"
            params = [status, now_ts, str(order_id)]

        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
            
        cursor.execute(sql, tuple(params))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update order status {order_id}: {e}")

def update_order_fill(order_id, filled_qty, bot_id=None):
    """Updates the filled_amount of an order in bot_orders."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        if bot_id:
            cursor.execute("""
                UPDATE bot_orders 
                SET filled_amount = ?, updated_at = ?
                WHERE order_id = ? AND bot_id = ?
            """, (filled_qty, int(time.time()), str(order_id), bot_id))
        else:
            cursor.execute("""
                UPDATE bot_orders 
                SET filled_amount = ?, updated_at = ?
                WHERE order_id = ?
            """, (filled_qty, int(time.time()), str(order_id)))
            
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update order fill: {e}")

def save_bot_order(bot_id, order_type, exchange_order_id, price, amount, step, status='open', client_order_id=None, notes=None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # CYCLE-ID STAMP: Read the bot's current cycle_id from trades.
        # Every order placed is tagged with the cycle it belongs to.
        # The Reconciler uses this to ONLY adopt fills from the current cycle.
        # If not in trade, we can still stamp it with what WILL be the current cycle
        cursor.execute("""
            SELECT COALESCE(
                (SELECT cycle_id FROM trades WHERE bot_id=?),
                (SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id=?)
            ) + CASE WHEN (SELECT 1 FROM trades WHERE bot_id=?) IS NULL THEN 1 ELSE 0 END
        """, (bot_id, bot_id, bot_id))
        row = cursor.fetchone()
        cycle_id = row[0] if row and row[0] else 1

        # 🚀 FUNDAMENTAL FIX: If the exchange immediately returns 'closed' or 'filled' on placement
        # (e.g. crossing the spread as a taker), we must stamp filled_amount immediately so 
        # the net_qty math in reset_bot_after_tp is balanced, even if websockets/reconciler misses it.
        initial_fill = amount if status.lower() in ('filled', 'closed') else 0.0

        cursor.execute("INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, notes, cycle_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (bot_id, step, order_type, exchange_order_id, price, amount, initial_fill, status, int(time.time()), client_order_id, notes, cycle_id))
        row_id = cursor.lastrowid
        
        # CRITICAL FIX: Also update trades table so get_bot_order_ids() / Guardian can see them.
        # Previously only bot_orders was written, but Guardian reads trades.entry_order_id/tp_order_id.
        if order_type == 'entry':
            cursor.execute("UPDATE trades SET entry_order_id = ? WHERE bot_id = ?", (exchange_order_id, bot_id))
        elif order_type == 'tp':
            cursor.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (exchange_order_id, bot_id))
        
        conn.commit()
        return row_id  # 🚀 Return row ID for pre-commit update pattern (callers use this to stamp the real order_id after exchange call)
    except Exception as e:
        logger.error(f"Failed to save bot order: {e}")
        return None

# NOTE: update_order_status is defined earlier (L1393) — correct version with updated_at timestamp.

def update_bot_order_exchange_id(db_row_id, exchange_order_id, status='open'):
    """
    🚀 PRE-COMMIT PATTERN: After placing an order on the exchange, call this to stamp the real
    exchange order_id onto the 'placing' row that was pre-committed before the exchange call.
    If exchange_order_id is None (order placement failed), marks the row as 'failed'.
    """
    if db_row_id is None:
        return False
    try:
        conn = get_connection()
        if exchange_order_id:
            conn.execute(
                "UPDATE bot_orders SET order_id=?, status=?, updated_at=? WHERE id=?",
                (exchange_order_id, status, int(time.time()), db_row_id)
            )
        else:
            conn.execute(
                "UPDATE bot_orders SET status='failed', updated_at=? WHERE id=?",
                (int(time.time()), db_row_id)
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update bot order exchange id: {e}")
        return False

def cancel_order_in_db(order_id):
    try:
        conn = get_connection()
        conn.execute("UPDATE bot_orders SET status = 'canceled', updated_at = ? WHERE order_id = ?", (int(time.time()), order_id))
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
        c.execute("SELECT price, amount, step, created_at FROM bot_orders WHERE bot_id = ? AND order_type = 'buy' AND status IN ('filled', 'closed') AND created_at >= ? ORDER BY created_at DESC LIMIT 1", (bot_id, basket_start_time))
        row = c.fetchone()
        if row: return {'price': row[0], 'amount': row[1], 'step': row[2], 'timestamp': row[3]}
    except: pass
    return None

# Module-level counter for update_full_snapshot (separate from update_active_positions_snapshot)
_FULL_SNAP_EMPTY_COUNTER = 0

def update_full_snapshot(trade_updates: List[Dict[str, Any]], physical_positions: List[Dict[str, Any]]):
    """
    Atomically updates both the virtual (trades) and physical (active_positions) state.
    This is the fundamental fix to prevent UI race conditions.
    """
    global _FULL_SNAP_EMPTY_COUNTER
    conn = None
    try:
        conn = get_connection()
        # check if there are active connect before attempting to connect
        if not conn: return
        
        # SAFETY LATCH v2: Consecutive-miss counter
        # Protects against transient API failures, but allows clearing after sustained empty responses.
        if not physical_positions or len(physical_positions) == 0:
             current_count = conn.execute("SELECT COUNT(*) FROM active_positions").fetchone()[0]
             if current_count > 0:
                 _FULL_SNAP_EMPTY_COUNTER += 1
                 if _FULL_SNAP_EMPTY_COUNTER < _EMPTY_SNAP_THRESHOLD:
                     logger.warning(f"⚠️ [SAFETY-LATCH] Empty snapshot {_FULL_SNAP_EMPTY_COUNTER}/{_EMPTY_SNAP_THRESHOLD}. Keeping {current_count} existing positions.")
                     return
                 else:
                     logger.info(f"✅ [SAFETY-LATCH] {_FULL_SNAP_EMPTY_COUNTER} consecutive empty snapshots. Clearing {current_count} stale positions.")
                     _FULL_SNAP_EMPTY_COUNTER = 0
        else:
            _FULL_SNAP_EMPTY_COUNTER = 0  # Reset on non-empty snapshot
        
        # FIX: Ensure we control the transaction explicitly
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        
        # 1. Update Virtual Positions
        for update in trade_updates:
            bot_id = update['bot_id']
            # Using UPSERT logic for robustness
            conn.execute("""
                INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, basket_start_time, current_step)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    total_invested = excluded.total_invested,
                    avg_entry_price = excluded.avg_entry_price,
                    entry_confirmed = excluded.entry_confirmed,
                    current_step = excluded.current_step
                WHERE bot_id = ? AND (current_step <= excluded.current_step OR excluded.current_step = 0)
            """, (bot_id, update['total_invested'], update['avg_entry_price'], update['entry_confirmed'], update['basket_start_time'], update.get('current_step', 0), bot_id))

        # Note: The empty-snapshot check is now handled by the consecutive-miss counter above.
        # If we reach here with empty positions, the counter already approved the clear.

        # 2. Update Physical Positions
        conn.execute("DELETE FROM active_positions")
        # print("DEBUG: Active Positions Deleted")
        
        for p in physical_positions:
            raw_symbol = p.get('symbol', 'UNKNOWN')
            
            # 🚀 FUNDAMENTAL FIX: Use central normalizer
            from engine.exchange_interface import normalize_symbol
            symbol = normalize_symbol(raw_symbol)
            
            # Use 'contracts' (futures) or 'amount' (spot) or 'size'
            amount = float(p.get('contracts', 0) or p.get('amount', 0) or p.get('size', 0) or 0)
            
            # ⚠️ CRITICAL FIX: explicit side
            p_side = p.get('side', '').lower()
            if p_side == 'short':
                side = 'SHORT'
            elif p_side == 'long':
                side = 'LONG'
            else:
                side = 'LONG' if amount > 0 else 'SHORT'
                
            entry_price = float(p.get('entryPrice', 0) or 0)
            
            # print(f"DEBUG: Processing {symbol} Amount: {amount}")
            
            if amount != 0:
                # Proactively link physical positions to active bots instead of defaulting to 0
                owner_id = get_active_bot_id_by_symbol_direction(symbol, side) or 0
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (owner_id, symbol, side, abs(amount), entry_price, int(time.time())))
                    # print(f"DEBUG: Inserted {symbol}")
                except Exception as insert_err:
                    logger.error(f"❌ INSERT FAILED for {symbol}: {insert_err}")
                    # print(f"DEBUG: Insert Failed: {insert_err}")
        
        conn.commit()
        # WAL Checkpoint removed (DELETE Mode)
        # conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        
        # logger.info(f"✅ Transactional snapshot update complete. Physical positions written: {len(physical_positions)}")
        
        # logger.info(f"✅ Transactional snapshot update complete. Physical positions written: {len(physical_positions)}")

    except Exception as e:
        logger.error(f"Failed to execute transactional snapshot update: {e}")
        if conn:
            try:
                conn.rollback()
            except: pass
    finally:
        if conn:
            try:
                conn.close()
            except: pass
