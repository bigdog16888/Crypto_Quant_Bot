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
        try:
            _local.connection = sqlite3.connect(DB_PATH, timeout=60.0)
            # ENABLE WAL MODE for enterprise concurrency safety
            _local.connection.execute("PRAGMA journal_mode=WAL")
            _local.connection.execute("PRAGMA synchronous=NORMAL")
            _local.connection.execute("PRAGMA busy_timeout=60000")
        except Exception as e:
            logger.error(f"❌ Failed to connect to database: {e}")
            return None
    
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
    2. bots with current_step > 0 but total_invested is 0,
    and 3. (v2.2) bots with step=0, invested=0 but open_qty > 0 (phantom accumulator).
    """
    try:
        c = conn.cursor()
        # v2.2: include open_qty so we can detect phantom accumulator values
        c.execute('''
            SELECT t.bot_id, b.pair, t.total_invested, t.avg_entry_price, t.current_step, t.cycle_id,
                   COALESCE(t.open_qty, 0) as open_qty
            FROM trades t JOIN bots b ON t.bot_id = b.id
        ''')
        trades = c.fetchall()
        for t in trades:
            bot_id, pair, invested, avg_price, step, cycle_id, open_qty = t
            invested = float(invested or 0)
            avg_price = float(avg_price or 0)
            step = int(step or 0)
            open_qty = float(open_qty or 0)

            # Scenario 1: Ghost step stuck (0 physical investment, but step > 0)
            # This causes the "0/2 limit orders missing" alert
            # Guard: skip if open_qty > 0 — the WS fill loop credited a real fill and
            # seal is still propagating. Wiping here would erase a confirmed position.
            if step > 0 and invested <= 0.0001 and open_qty <= 0.0001:
                c.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, open_qty = 0, cycle_id = NULL WHERE bot_id = ?", (bot_id,))
                conn.commit()
                logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Reset stranded ghost step back to 0. Cleared metrics.")
                continue

            # Scenario 3: Phantom Invested Amount (Stuck metrics on a Scanning Bot)
            # Corrects databases where step was manually reverted to 0 but metrics weren't cleared.
            # GUARD: skip if open_qty > 0 — the WS fill loop credited a real position fill;
            # seal wrote step=0 because cycle_id=NULL broke recompute_invested_from_orders.
            # Wiping here would erase a confirmed physical position.
            if step == 0 and (invested > 0.001 or avg_price > 0.001) and open_qty <= 0.0001:
                c.execute("UPDATE trades SET total_invested = 0, avg_entry_price = 0, target_tp_price = 0, open_qty = 0, cycle_id = NULL WHERE bot_id = ?", (bot_id,))
                conn.commit()
                logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Purged phantom ${invested:.2f} invested memory on a SCANNING bot.")
                continue

            # Scenario 4 (v2.2): Phantom open_qty on a Scanning bot (step=0, invested=0, open_qty > 0)
            # Root cause: sync_exchange_to_db.py previously placed raw market orders that bypassed
            # credit_fill(), leaving the accumulator frozen. Also triggered by the integrity bootstrap
            # setting open_qty from recompute without a corresponding total_invested update.
            # FIX: Check bot_orders for a real net fill backing this qty. If found → seal_trade_state
            # will correctly propagate it. If not found → it is a phantom; zero it.
            if step == 0 and invested <= 0.0001 and open_qty > 0.0001:
                ledger_net = c.execute("""
                    SELECT COALESCE(SUM(
                        CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount
                             WHEN order_type IN ('tp','close','adoption_reduce','dust_close','sl') THEN -filled_amount
                             ELSE 0 END
                    ), 0)
                    FROM bot_orders
                    WHERE bot_id = ? AND filled_amount > 0
                      AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed')
                """, (bot_id,)).fetchone()[0]
                ledger_net = max(0.0, float(ledger_net or 0))
                if ledger_net > 0.0001:
                    # Real fills exist in bot_orders — sync_trades_from_orders will propagate correctly.
                    # 🚀 ROOT CAUSE FIX (v2.3.1): If cycle_id is NULL in trades, recompute_invested_from_orders
                    # queries `WHERE cycle_id = NULL` which matches nothing in SQL, returning 0 forever.
                    # Restore cycle_id from the highest cycle in bot_orders BEFORE sealing.
                    trades_cycle_row = c.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                    trades_cycle_id = trades_cycle_row[0] if trades_cycle_row else None
                    if trades_cycle_id is None:
                        backing_cycle = c.execute(
                            "SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id=? AND filled_amount > 0 "
                            "AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed')",
                            (bot_id,)
                        ).fetchone()[0]
                        if backing_cycle is not None:
                            c.execute("UPDATE trades SET cycle_id=? WHERE bot_id=?", (backing_cycle, bot_id))
                            conn.commit()
                            logger.warning(
                                f"🩹 [CYCLE-RESTORE] Bot {bot_id} ({pair}): trades.cycle_id was NULL. "
                                f"Restored to cycle {backing_cycle} from bot_orders. This was causing phantom open_qty."
                            )
                    conn.commit()
                    sync_trades_from_orders(bot_id)
                    logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Phantom open_qty={open_qty:.8f} backed by {ledger_net:.8f} bot_orders net — triggered seal.")
                else:
                    # No bot_orders basis → this is a true ghost accumulator; zero it
                    c.execute("UPDATE trades SET open_qty = 0 WHERE bot_id = ?", (bot_id,))
                    conn.commit()
                    logger.info(f"🩹 [HEALED] Bot {bot_id} ({pair}): Zeroed phantom open_qty={open_qty:.8f} — no backing fills in bot_orders.")
                continue

            if invested > 0.0001 and avg_price > 0:
                # Replace legacy manual SQL gap-check with the single-source-of-truth function
                # This ensures any drift is immediately healed using the proof-only reconciliation engine
                conn.commit()  # Release write lock before calling external connection function
                sync_trades_from_orders(bot_id)
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
                normalized_pair TEXT,
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
                last_error_time INTEGER,
                pos_limit_hit INTEGER DEFAULT 0
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
            conn.commit() # FINAL COMMIT for this table
            logger.info("🛠️ Database Migration: Added last_error columns to bots table.")

        # 🚀 FUNDAMENTAL FIX: Normalized Pair Standard (V1.6.4)
        try:
            cursor.execute('SELECT normalized_pair FROM bots LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bots ADD COLUMN normalized_pair TEXT")
            conn.commit()
            logger.info("🛠️ Database Migration: Added normalized_pair column to bots table.")
            # Automatic backfill
            from engine.exchange_interface import normalize_symbol
            cursor.execute("SELECT id, pair FROM bots")
            all_bots = cursor.fetchall()
            for b_id, b_pair in all_bots:
                norm = normalize_symbol(b_pair)
                cursor.execute("UPDATE bots SET normalized_pair = ? WHERE id = ?", (norm, b_id))
            conn.commit()
            logger.info(f"✅ Backfilled normalized_pair for {len(all_bots)} bots.")
        
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
                cycle_id INTEGER DEFAULT 1,
                cycle_phase TEXT DEFAULT 'ACTIVE',
                open_qty REAL DEFAULT 0,
                wipe_wall_ts INTEGER DEFAULT 0,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        conn.commit() # Ensure table exists before migrations
        
        # Migrations for new columns
        try:
            cursor.execute('SELECT last_exit_price FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_price REAL DEFAULT 0')
            cursor.execute('ALTER TABLE trades ADD COLUMN last_exit_time INTEGER DEFAULT 0')
            conn.commit()

        # Add cycle_id column (Phase 8 Legacy Healing)
        try:
            cursor.execute('SELECT cycle_id FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN cycle_id INTEGER DEFAULT 1')
            conn.commit()
            logger.info("🛠️ DB Migration: Added cycle_id column to trades table.")

        # Add close_type column (single column, no UNIQUE)
        try:
            cursor.execute('SELECT close_type FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN close_type TEXT DEFAULT NULL')
            conn.commit()

        # ── ARCHITECTURAL FIX (Phase 3) ──────────────────────────────────────
        # cycle_phase: tracks the lifecycle state of the current trading cycle.
        # Values: 'ACTIVE'  — normal trading, ghost-checks apply
        #         'CARRY_PENDING' — partial fill carried from previous cycle;
        #                           basket_start_time is fresh but no new fills
        #                           exist yet. Ghost-checks MUST skip this state.
        #         'IDLE'    — no open position (equivalent to total_invested=0)
        # This eliminates the race condition where a CARRY bot is wiped because
        # the ghost-check sees 0 bot_orders since basket_start_time.
        # ─────────────────────────────────────────────────────────────────────
        try:
            cursor.execute('SELECT cycle_phase FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trades ADD COLUMN cycle_phase TEXT DEFAULT 'ACTIVE'")
            conn.commit()
            logger.info("🛠️ DB Migration: Added cycle_phase column to trades table (ARCHITECTURAL FIX).")

        # 🚀 HEDGE-MODE FIX: position_side column in trades
        try:
            cursor.execute('SELECT position_side FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trades ADD COLUMN position_side TEXT DEFAULT 'BOTH'")
            conn.commit()
            logger.info("🛠️ DB Migration: Added position_side column to trades table.")
            # Backfill from bots table
            cursor.execute("""
                UPDATE trades SET position_side = (
                    SELECT CASE WHEN direction='SHORT' THEN 'SHORT' ELSE 'LONG' END 
                    FROM bots WHERE bots.id = trades.bot_id
                )
            """)
            conn.commit()

        # ── v2.1.0 CYCLE TIMESTAMP ARCHITECTURE ──────────────────────────────────
        # cycle_start_time: unix timestamp (seconds) of the exchange event that
        # STARTED this cycle (the TP fill that ended the previous one, or the first
        # fill adoption timestamp for new bots).  This is the authoritative cycle
        # boundary — NOT basket_start_time which is an engine-operation timestamp.
        # Immutable for the life of the cycle; only updated when cycle_id increments.
        try:
            cursor.execute('SELECT cycle_start_time FROM trades LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE trades ADD COLUMN cycle_start_time INTEGER DEFAULT 0')
            conn.commit()
            logger.info("🛠️ DB Migration v2.1.0: Added cycle_start_time column to trades table.")
            # Backfill: use last_exit_time as best available approximation for existing rows.
            # For rows with no prior exit (first cycle ever), this stays 0 — correct behaviour
            # (0 means no boundary, reconciler will not demote any fills).
            cursor.execute("""
                UPDATE trades SET cycle_start_time = last_exit_time
                WHERE last_exit_time > 0 AND cycle_start_time = 0
            """)
            conn.commit()
            logger.info("🛠️ DB Migration v2.1.0: Backfilled cycle_start_time from last_exit_time.")
        # ─────────────────────────────────────────────────────────────────────────

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

        # Migration for cycle_id in bot_orders (Phase 8 Consistency)
        try:
            cursor.execute('SELECT cycle_id FROM bot_orders LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE bot_orders ADD COLUMN cycle_id INTEGER')
            conn.commit()
            logger.info("🛠️ DB Migration: Added cycle_id column to bot_orders table.")

        # 🚀 HEDGE-MODE FIX: position_side column in bot_orders
        try:
            cursor.execute('SELECT position_side FROM bot_orders LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE bot_orders ADD COLUMN position_side TEXT DEFAULT 'BOTH'")
            conn.commit()
            logger.info("🛠️ DB Migration: Added position_side column to bot_orders table.")
            # Backfill
            cursor.execute("""
                UPDATE bot_orders SET position_side = (
                    SELECT CASE WHEN direction='SHORT' THEN 'SHORT' ELSE 'LONG' END 
                    FROM bots WHERE bots.id = bot_orders.bot_id
                )
            """)
            conn.commit()

        # ── v2.1.0 FILL TIMESTAMP ─────────────────────────────────────────────────
        # filled_at: unix timestamp (seconds) from the exchange when this order was
        # actually executed (lastTradeTimestamp from Binance WS/REST). Stored in
        # seconds for consistency with all other timestamp columns.
        # This is the permanent, immutable audit record of WHEN the fill occurred.
        # Used by recompute_invested_from_orders and the reconciler cycle guard.
        try:
            cursor.execute('SELECT filled_at FROM bot_orders LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE bot_orders ADD COLUMN filled_at INTEGER DEFAULT 0')
            conn.commit()
            logger.info("🛠️ DB Migration v2.1.0: Added filled_at column to bot_orders table.")
            # Backfill: updated_at was set at fill-confirmation time — good approximation.
            cursor.execute("""
                UPDATE bot_orders SET filled_at = updated_at
                WHERE status IN ('filled', 'closed', 'partially_filled', 'reset_cleared')
                  AND updated_at > 0 AND filled_at = 0
            """)
            conn.commit()
            logger.info("🛠️ DB Migration v2.1.0: Backfilled filled_at from updated_at for confirmed fills.")
        # ─────────────────────────────────────────────────────────────────────────

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_bot ON bot_orders(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_order_id ON bot_orders(order_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_client_id ON bot_orders(client_order_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_filled_at ON bot_orders(bot_id, filled_at)')
        
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
                position_side TEXT DEFAULT 'BOTH',
                pnl REAL DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
            )
        """)
        
        # Migration for position_side in trade_history
        try:
            cursor.execute('SELECT position_side FROM trade_history LIMIT 1')
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trade_history ADD COLUMN position_side TEXT DEFAULT 'BOTH'")
            conn.commit()
            # Backfill
            cursor.execute("""
                UPDATE trade_history SET position_side = (
                    SELECT CASE WHEN direction='SHORT' THEN 'SHORT' ELSE 'LONG' END 
                    FROM bots WHERE bots.id = trade_history.bot_id
                )
            """)
            conn.commit()
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_time ON trade_history(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_active ON bots(is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bots_pair ON bots(pair)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_status ON bot_orders(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bot_orders_type ON bot_orders(order_type)')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uniq_exchange_oid ON bot_orders(order_id) WHERE order_id IS NOT NULL AND order_id != ''")
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
        # Re-create to ensure schema update (side-aware PK)
        cursor.execute('DROP TABLE IF EXISTS active_positions')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_positions (
                bot_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
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
        
        # 🛡️ ARCHITECT'S SHIELD: Manual Whitelisting
        # Stores user-declared manual trades that the bot should ignore.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS manual_whitelists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_whitelist_pair ON manual_whitelists(pair)')

        # FUNDAMENTAL FIX: Clear stale active positions on startup
        # This prevents the UI from showing "Green" (Synced) against old data before the first poll cycle
        cursor.execute('DELETE FROM active_positions')
        conn.commit()  # Release lock before external sync

        heal_zombie_bots(conn)
    except Exception as e:
        try:
            logger.warning(f"Database init warning (non-fatal): {e}")
        except:
            pass
        return
    finally:
        if conn is not None:
            try:
                pass # conn.close() disabled for singleton safety
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
    # 🚀 ROOT CAUSE FIX: Direction (and key sizing params) MUST be inside the config JSON blob.
    # The strategy reads params.get('direction') from config, NOT from the DB column.
    # If we only store direction in the bots.direction column and not in config JSON,
    # the strategy defaults to 'LONG' for every bot regardless of what the user configured.
    config_dict['direction'] = direction.upper()
    config_dict['base_size'] = config_dict.get('base_size', base_size)
    config_dict['martingale_multiplier'] = config_dict.get('martingale_multiplier', martingale_multiplier)
    config_dict['bot_name'] = name  # Useful for logging inside strategy
    config_json = json.dumps(config_dict)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass # Already in transaction
    cursor = conn.cursor()
    try:
        from engine.exchange_interface import normalize_symbol
        norm_pair = normalize_symbol(pair)
        cursor.execute("INSERT INTO bots (name, pair, normalized_pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Scanning')", (name, pair, norm_pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json))
        bot_id = cursor.lastrowid
        cursor.execute('INSERT INTO trades (bot_id) VALUES (?)', (bot_id,))
        conn.commit()
        return bot_id
    except sqlite3.IntegrityError:
        conn.rollback()
        logger.warning(f"Error: Bot name '{name}' already exists.")
        return None
    except Exception as e:
        conn.rollback()
        logger.error(f"Error adding bot: {e}")
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
               COALESCE(b.pos_limit_hit, 0) as pos_limit_hit,
               COALESCE(t.cycle_phase, 'ACTIVE') as cycle_phase,
               COALESCE(t.cycle_start_time, 0) as cycle_start_time,
               COALESCE(t.open_qty, 0) as open_qty
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
        'cycle_phase': row[15],
        'cycle_start_time': row[16],   # v2.1.0 — authoritative cycle boundary
        'open_qty': row[17],           # v2.1.0 — running confirmed position qty
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
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        from engine.exchange_interface import normalize_symbol
        norm_pair = normalize_symbol(pair)
        cursor.execute("UPDATE bots SET name=?, pair=?, normalized_pair=?, direction=?, rsi_limit=?, martingale_multiplier=?, base_size=?, strategy_type=?, config=? WHERE id=?", (name, pair, norm_pair, direction.upper(), rsi_limit, martingale_multiplier, base_size, strategy_type, config_json, bot_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        try: conn.rollback()
        except: pass
        logger.warning(f"Error: Bot name '{name}' already exists.")
        return False
    except Exception as e:
        try: conn.rollback()
        except: pass
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
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        
        # Check if trade record exists
        cursor.execute("SELECT current_step FROM trades WHERE bot_id = ?", (bot_id,))
        exists = cursor.fetchone()
        
        if exists:
            # Look up bot direction for position_side
            cursor.execute("SELECT direction FROM bots WHERE id = ?", (bot_id,))
            bot_dir_row = cursor.fetchone()
            position_side = str(bot_dir_row[0]).upper() if bot_dir_row else 'LONG'
            
            # UPDATE existing record
            cursor.execute("""
                UPDATE trades
                SET current_step = ?, 
                    total_invested = ?, 
                    avg_entry_price = ?, 
                    target_tp_price = ?,
                    entry_confirmed = 1,
                    position_side = ?
                WHERE bot_id = ? AND (current_step <= ? OR ? = 0)
            """, (step, total_invested, avg_price, tp_price, position_side, bot_id, step, step))
            logger.debug(f"✅ Updated trade state for bot {bot_id}: step={step}, invested={total_invested}, avg_price={avg_price}")
        else:
            # Look up bot direction for position_side
            cursor.execute("SELECT direction FROM bots WHERE id = ?", (bot_id,))
            bot_dir_row = cursor.fetchone()
            position_side = str(bot_dir_row[0]).upper() if bot_dir_row else 'LONG'
            
            # INSERT new record
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price, entry_confirmed, basket_start_time, position_side)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (bot_id, step, total_invested, avg_price, tp_price, int(time.time()), position_side))
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
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute("UPDATE bots SET is_active = 0, status='STOPPED' WHERE id = ?", (bot_id,))
        log_trade(bot_id, 'ERROR_STOP', 'SYSTEM', 0, 0, 0, "SYS_STOP", 0, f"Auto-Stopped: {reason}")
        conn.commit()
        logger.info(f"Bot {bot_id} deactivated: {reason}")
        return True
    except Exception as e:
        logger.error(f"Failed to deactivate bot {bot_id}: {e}")
        try: conn.rollback()
        except: pass
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

def _reset_bot_after_tp_internal(cursor, bot_id, exit_price, direction=None, action_label='TP_HIT', notes='', exit_fill_ts: int = 0):
    """
    Internal implementation of reset_bot_after_tp.
    Assumes an active transaction cursor is passed in. Does NOT call conn.commit().

    Args:
        exit_fill_ts: Unix timestamp (seconds) of the exchange fill event that triggered
                      this reset (e.g. TP order's lastTradeTimestamp / 1000).
                      This becomes cycle_start_time for the NEW cycle — the authoritative
                      boundary that the reconciler uses for fill attribution.
                      Defaults to int(time.time()) if not provided.
    """
    cursor.execute("SELECT total_invested, current_step, avg_entry_price, name, pair, direction, config FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
    row = cursor.fetchone()
    if not row: return
    total_invested, current_step, avg_entry_price, bot_name, pair, db_direction, config_str = row
    
    final_direction = direction or db_direction or 'LONG'
    pnl = 0.0
    if exit_price > 0 and avg_entry_price > 0:
        est_qty = total_invested / avg_entry_price
        if final_direction.upper() == 'LONG':
            pnl = (exit_price - avg_entry_price) * est_qty
        else:
            pnl = (avg_entry_price - exit_price) * est_qty
    
    # Use cursor-based internal log to avoid nested transactions
    _log_trade_internal(cursor, bot_id, action_label, pair, exit_price, total_invested / avg_entry_price if avg_entry_price > 0 else 0, total_invested, step=current_step, pnl=pnl, notes=notes, position_side=final_direction)
    
    cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,))
    cycle_row = cursor.fetchone()
    old_cycle = int(cycle_row[0]) if cycle_row and cycle_row[0] else 1
    new_cycle = old_cycle + 1


    cursor.execute("""
        SELECT ROUND(COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount ELSE 0 END), 0) -
               COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0), 8)
        FROM bot_orders WHERE bot_id = ? AND filled_amount > 0 AND (cycle_id = ? OR cycle_id IS NULL)
        AND status NOT IN ('reset_cleared', 'auto_closed')
    """, (bot_id, old_cycle))
    raw_net = cursor.fetchone()[0] or 0.0
    # Snap sub-epsilon dust to zero to prevent ghost carry-over
    old_net_qty = max(0.0, float(raw_net) if abs(float(raw_net)) > 1e-8 else 0.0)

    cursor.execute("UPDATE bot_orders SET status = 'auto_closed', updated_at = ? WHERE bot_id = ? AND status IN ('open', 'new')", (int(time.time()), bot_id))
    cursor.execute("UPDATE bot_orders SET status = 'reset_cleared', updated_at = ? WHERE bot_id = ? AND status NOT IN ('auto_closed', 'reset_cleared') AND order_type != 'hedge'", (int(time.time()), bot_id))

    # 🚀 ROOT CAUSE FIX B: Clamp carry-over qty against exchange physical reality.
    # Previously, old_net_qty was pure arithmetic from bot_orders (entry fills - TP fills).
    # If a TP partially filled and left dust, or fills came from multiple cycles, the
    # arithmetic could exceed the real exchange position — injecting a ghost CARRY row.
    # Cross-check against active_positions (the exchange snapshot) and clamp if needed.
    bot_dir_upper = str(final_direction).upper()
    phys_side = 'LONG' if bot_dir_upper == 'LONG' else 'SHORT'
    norm_pair = pair.split(':')[0].replace('/', '')
    snap_row = cursor.execute(
        "SELECT size FROM active_positions WHERE pair=? AND side=?",
        (norm_pair, phys_side)
    ).fetchone()
    if snap_row and float(snap_row[0] or 0) > 0:
        phys_qty = float(snap_row[0])
        if old_net_qty > phys_qty * 1.05:  # 5% tolerance for rounding/lot-size
            logger.warning(
                f"🔒 [CARRY-CLAMP] Bot {bot_id}: Clamping carry "
                f"{old_net_qty:.6f} → {phys_qty:.6f} {pair} "
                f"(exchange holds only {phys_qty:.6f}, avoiding ghost carry-over)"
            )
            old_net_qty = phys_qty
    elif snap_row is None or (snap_row and float(snap_row[0] or 0) == 0):
        # No physical position on exchange — reset or already flat.
        # Suppress the CARRY entirely to avoid ghost injection.
        if old_net_qty > 0.0001:
            logger.warning(
                f"🔒 [CARRY-SUPPRESS] Bot {bot_id}: arithmetic carry={old_net_qty:.6f} "
                f"but exchange has NO {phys_side} {pair} position. Suppressing carry-over."
            )
            old_net_qty = 0.0

    excluded_carry_labels = ['RESET_VANISHED_POSITION', 'RESET_STRUCTURAL_GHOST', 'RESET_PHANTOM_ENTRY', 'SYSTEM_WIPE', 'MANUAL_CLOSE', 'STOP_LOSS_EXIT']
    
    # 🛡️ DUST-AWARE COMPLETION (ROOT CAUSE FIX):
    # Only carry over residiual quantity if it represents significant monetary value (>$1.00).
    # This prevents the "Impossible Loop" where tiny rounding errors block 
    # the bot from returning to 'Scanning' mode.
    residue_notional = abs(old_net_qty) * (exit_price if exit_price > 0 else 1.0)
    
    if abs(old_net_qty) > 0.0001 and residue_notional > 1.0 and action_label not in excluded_carry_labels:
        logger.info(f"🌉 [CARRY-OVER] Bot {bot_id}: Carrying over {old_net_qty:.4f} {pair} units into Cycle {new_cycle}.")
        carry_otype = 'entry'
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
        cursor.execute("UPDATE bot_orders SET status = 'filled' WHERE client_order_id = ?", (carry_cid,))

    new_cycle_phase = 'CARRY_PENDING' if (abs(old_net_qty) > 0.0001 and residue_notional > 1.0 and action_label not in excluded_carry_labels) else 'IDLE'
    # Determine cycle_start_time for the new cycle:
    # Use the actual exchange fill timestamp if provided; fall back to now.
    # This is the authoritative cycle boundary for all future fill attribution.
    new_cycle_start_time = exit_fill_ts if exit_fill_ts > 0 else int(time.time())

    cursor.execute(
        "UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, "
        "target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = ?, "
        "entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, "
        "bot_position_id = NULL, close_type = ?, cycle_id = ?, cycle_phase = ?, "
        "open_qty = 0, wipe_wall_ts = ?, cycle_start_time = ? WHERE bot_id = ?",
        (exit_price, int(time.time()), int(time.time()), action_label, new_cycle, new_cycle_phase,
         int(time.time()), new_cycle_start_time, bot_id)
    )
    logger.info(
        f"🕐 [CYCLE-START] Bot {bot_id}: New cycle {new_cycle} anchored at "
        f"cycle_start_time={new_cycle_start_time} "
        f"({'exchange fill ts' if exit_fill_ts > 0 else 'engine time fallback'})"
    )

    cursor.execute("UPDATE bots SET pos_limit_hit = 0 WHERE id = ?", (bot_id,))
    try:
        clear_active_position_for_bot(bot_id, pair, cursor=cursor)
    except Exception as e_ap:
        logger.warning(f"[ACTIVE-POS] Could not clear active_positions for bot {bot_id}: {e_ap}")
    
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


def reset_bot_after_tp(bot_id, exit_price, direction=None, action_label='TP_HIT', notes='', exit_fill_ts: int = 0):
    """
    Public wrapper that manages its own transaction.

    Args:
        exit_fill_ts: Unix timestamp (seconds) of the exchange fill event.
                      Passed through to _reset_bot_after_tp_internal to anchor
                      cycle_start_time to the actual trade execution time.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        _reset_bot_after_tp_internal(cursor, bot_id, exit_price, direction, action_label, notes, exit_fill_ts=exit_fill_ts)
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except: pass
        logger.error(f"Error resetting trade for bot {bot_id}: {e}")
        raise


# =============================================================================
# ARCHITECTURAL GATE: safe_wipe_bot()
# =============================================================================
# ALL code that wants to reset a bot's position must call this function.
# Direct calls to reset_bot_after_tp(..., action_label='SYSTEM_WIPE') are
# FORBIDDEN outside of this function. This is the single enforcement point.
#
# Rules:
#   1. Never wipe if exchange physical qty > MIN_PHYSICAL_THRESHOLD.
#      (Real-time check against active_positions OR live exchange snapshot)
#   2. Never wipe if cycle_phase == 'CARRY_PENDING'.
#      (Carry bots look like ghosts but have real money.)
#   3. Never wipe if bot_orders ledger sum > threshold in ANY cycle
#      (not just the current one).
#   4. If all 3 rules pass → wipe is safe → call reset_bot_after_tp.
# =============================================================================
def safe_wipe_bot(
    bot_id: int,
    pair: str,
    direction: str,
    reason: str,
    exit_price: float = 0.0,
    exchange_snapshot: Optional[dict] = None,  # Optional real-time snapshot from caller
    force: bool = False,                         # Override all guards — manual wipe only
    bypass_ledger_guard: bool = False            # Top-down override: true if physical == 0
) -> bool:
    """
    Centralized gate for all destructive bot resets.

    Returns True if the wipe was executed, False if it was blocked.
    """
    # Treatment of positions < $1.00 as "Dust" to allow state transitions
    MIN_NOTIONAL_THRESHOLD = 1.0  # Dollars

    conn = get_connection()
    cursor = conn.cursor()

    # ── Guard 0: Force mode (manual admin wipe only) ──────────────────────
    if force:
        logger.warning(
            f"⚠️ [SAFE-WIPE] FORCE override for bot {bot_id} ({pair} {direction}). "
            f"Skipping all safety checks. Reason: {reason}"
        )
        reset_bot_after_tp(bot_id, exit_price, direction=direction, action_label='SYSTEM_WIPE')
        return True

    # ── Guard 1: cycle_phase == CARRY_PENDING ────────────────────────────
    row = cursor.execute(
        "SELECT cycle_phase, total_invested FROM trades WHERE bot_id=?", (bot_id,)
    ).fetchone()
    if row:
        cycle_phase = row[0] or 'ACTIVE'
        total_invested = float(row[1] or 0)
        if cycle_phase == 'CARRY_PENDING' and not bypass_ledger_guard:
            logger.warning(
                f"🛡️ [SAFE-WIPE BLOCKED] Bot {bot_id} is CARRY_PENDING. "
                f"This is NOT a ghost — it's a carried position waiting for fills. "
                f"Wipe blocked. Reason attempted: {reason}"
            )
            return False
    else:
        total_invested = 0.0

    # ── Guard 2: Physical position on exchange ───────────────────────────
    # First check active_positions table (fast, cached from last snapshot)
    from engine.exchange_interface import normalize_symbol
    clean_pair = normalize_symbol(pair)
    side_check = direction.upper()

    cached_phys_qty = 0.0
    phys_row = cursor.execute(
        "SELECT ABS(size) FROM active_positions WHERE pair=? AND side=?",
        (clean_pair, side_check)
    ).fetchone()
    if phys_row and phys_row[0]:
        cached_phys_qty = float(phys_row[0])

    # If caller provided a fresh snapshot, use it (more reliable)
    snapshot_phys_qty = 0.0
    if exchange_snapshot:
        for pos in exchange_snapshot.get('positions', []):
            if (
                normalize_symbol(pos.get('symbol', '')) == clean_pair
                and pos.get('side', '').upper() == side_check
            ):
                snapshot_phys_qty += abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0)))

    phys_qty = max(cached_phys_qty, snapshot_phys_qty)

    # We now check NOTIONAL value instead of just quantity.
    cursor.execute("SELECT avg_entry_price FROM trades WHERE bot_id=?", (bot_id,))
    price_row = cursor.fetchone()
    current_price = float(price_row[0]) if price_row and price_row[0] else 0.0
    
    # If we have no price, we use 1.0 as multiplier (conservative)
    notional_value = phys_qty * (current_price if current_price > 0 else 1.0)

    if notional_value > MIN_NOTIONAL_THRESHOLD:
        logger.warning(
            f"🛡️ [SAFE-WIPE BLOCKED] Bot {bot_id} ({clean_pair} {side_check}): "
            f"Exchange shows {phys_qty:.6f} units (~${notional_value:.2f}). "
            f"Wipe BLOCKED — significant money is on exchange. Reason: {reason}"
        )
        return False

    # ── Guard 3: Ledger still shows fills (across ALL cycles) ────────────
    ledger_row = cursor.execute("""
        SELECT COALESCE(SUM(
            CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END
        ) - SUM(
            CASE WHEN order_type IN ('tp','close','adoption_reduce','dust_close','sl') THEN filled_amount ELSE 0 END
        ), 0)
        FROM bot_orders
        WHERE bot_id=? AND filled_amount > 0
        AND status NOT IN ('reset_cleared', 'auto_closed')
    """, (bot_id,)).fetchone()
    ledger_net_qty = max(0.0, float(ledger_row[0] or 0))

    # check notional for ledger too
    ledger_notional = ledger_net_qty * (current_price if current_price > 0 else 1.0)
    if ledger_notional > MIN_NOTIONAL_THRESHOLD:
        if bypass_ledger_guard:
            logger.warning(f"⚠️ [SAFE-WIPE BYPASS] Bot {bot_id}: Ledger shows ${ledger_notional:.2f} residue, but bypass is ACTIVE.")
        else:
            logger.warning(
                f"🛡️ [SAFE-WIPE BLOCKED] Bot {bot_id}: Ledger shows {ledger_net_qty:.6f} net units (~${ledger_notional:.2f}). "
                f"Wipe BLOCKED. Reason: {reason}"
            )
            return False

    # ── All guards passed — wipe is safe ─────────────────────────────────
    logger.info(
        f"✅ [SAFE-WIPE APPROVED] Bot {bot_id} ({clean_pair} {side_check}): "
        f"phys_qty={phys_qty:.6f}, ledger_net={ledger_net_qty:.6f}, "
        f"cycle_phase={row[0] if row else 'N/A'}. Executing wipe. Reason: {reason}"
    )
    reset_bot_after_tp(bot_id, exit_price, direction=direction, action_label='SYSTEM_WIPE')
    return True


def check_and_fix_integrity():
    """
    Sanitizes the database state on startup (or periodically).
    Fixes 'Zombie', 'Ghost', and 'Corrupted' (invalid prices) states.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # ── Step 0: Bootstrap missing trades rows ─────────────────────────────────
    # If the trades table was wiped or a bot was added without a matching row,
    # seal_trade_state silently returns {} (no-op), causing every guard to see
    # total_invested=0 and every entry to get ENTRY-ANCHOR or MAGNITUDE blocked.
    cursor.execute("""
        SELECT b.id, b.direction
        FROM bots b
        LEFT JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND t.bot_id IS NULL
    """)
    missing_trades = cursor.fetchall()
    for _bid, _dir in missing_trades:
        _side = 'SHORT' if 'short' in str(_dir or '').lower() else 'LONG'
        cursor.execute("""
            INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price,
                                entry_confirmed, basket_start_time, cycle_id, position_side)
            VALUES (?, 0, 0, 0, 0, 0, 1, ?)
        """, (_bid, _side))
        logger.warning(f"🔧 Integrity Fix: Created missing trades row for bot {_bid} (side={_side}).")
        fixed_count = fixed_count + 1 if 'fixed_count' in dir() else 1
    if missing_trades:
        conn.commit()
    # ──────────────────────────────────────────────────────────────────────────

    # ── Step 0.5: Bootstrap missing open_qty (v2.1) ───────────────────────────
    # For bots that were active before v2.1 migration, open_qty might be 0
    # even though they have an actual position. Backfill from recompute.
    cursor.execute("SELECT bot_id FROM trades WHERE COALESCE(open_qty, 0) <= 0 AND total_invested > 0")
    for (bid,) in cursor.fetchall():
        cost, avg, qty, step = recompute_invested_from_orders(bid)
        if qty > 0:
            cursor.execute("UPDATE trades SET open_qty=? WHERE bot_id=? AND COALESCE(open_qty, 0) <= 0", (qty, bid))
            logger.info(f"🔧 Integrity Fix: Backfilled open_qty={qty:.8f} for bot {bid} (v2.1 upgrade state).")
    conn.commit()
    # ──────────────────────────────────────────────────────────────────────────

    fixed_count = 0

    # 0. Fix Corrupted Data (The $9M PnL Bug)
    # Wipe any trade with impossible entry prices OR where total_invested / avg_entry_price != self-consistent
    cursor.execute("SELECT bot_id, name, avg_entry_price, total_invested FROM trades t JOIN bots b ON t.bot_id = b.id WHERE avg_entry_price > 0 AND total_invested > 0")
    corrupted_candidates = cursor.fetchall()

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
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute('UPDATE bots SET status = ? WHERE id = ?', (status, bot_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update bot status: {e}")
        try: conn.rollback()
        except: pass

def get_bot_order_ids(bot_id):
    conn = get_connection()
    cursor = conn.cursor()
    orders = {'entry_order_id': None, 'tp_order_id': None, 'grid_orders': []}
    
    # Primary source: trades table (fast, single row)
    cursor.execute('SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id = ?', (bot_id,))
    res = cursor.fetchone()
    if res:
        raw_entry, raw_tp = res
        orders['entry_order_id'] = raw_entry
        # 🚀 BUG FIX: Strip PLACING_ prefix from trades.tp_order_id.
        # The pre-commit pattern writes 'PLACING_{clientOrderId}' to trades before the exchange call.
        # update_bot_order_exchange_id only updates bot_orders, not trades, so trades can be
        # permanently stuck with the placeholder. Strip it here so the stalemate evictor
        # doesn't try to fetch 'PLACING_CQB_...' as an exchange order ID.
        if raw_tp and str(raw_tp).startswith('PLACING_'):
            # Try to find the real exchange ID in bot_orders for this bot's tp type
            cursor.execute(
                "SELECT order_id FROM bot_orders WHERE bot_id=? AND order_type='tp'"
                " AND status IN ('open','new','placed') AND order_id NOT LIKE 'PLACING_%'"
                " ORDER BY created_at DESC LIMIT 1",
                (bot_id,)
            )
            real_tp_row = cursor.fetchone()
            if real_tp_row and real_tp_row[0]:
                orders['tp_order_id'] = real_tp_row[0]
                # Back-fill trades so we don't hit this path again
                try:
                    conn.execute(
                        "UPDATE trades SET tp_order_id=? WHERE bot_id=? AND tp_order_id=?",
                        (real_tp_row[0], bot_id, raw_tp)
                    )
                    conn.commit()
                except Exception:
                    pass
            else:
                orders['tp_order_id'] = None  # Will trigger fresh TP placement
        else:
            orders['tp_order_id'] = raw_tp
    
    # BELT-AND-SUSPENDERS: If trades table has NULL, check bot_orders as fallback.
    if not orders['entry_order_id']:
        cursor.execute(
            "SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'entry'"
            " AND status IN ('open','new','placing') ORDER BY created_at DESC LIMIT 1",
            (bot_id,)
        )
        entry_row = cursor.fetchone()
        if entry_row and entry_row[0]:
            orders['entry_order_id'] = entry_row[0]
    
    if not orders['tp_order_id']:
        cursor.execute(
            "SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'tp'"
            " AND status IN ('open','new','placing') AND order_id NOT LIKE 'PLACING_%'"
            " ORDER BY created_at DESC LIMIT 1",
            (bot_id,)
        )
        tp_row = cursor.fetchone()
        if tp_row and tp_row[0]:
            orders['tp_order_id'] = tp_row[0]
    
    # Grid orders from bot_orders.
    # 🚀 BUG FIX: Include 'new' and 'placing' statuses.
    # Binance FAPI returns status='new' for acknowledged (not-yet-filled) limit orders.
    # 'placing' is the pre-commit placeholder written before the exchange call.
    # Both represent "this grid is tracked and on the exchange" — treating only 'open'
    # causes local_grid_ids to be [], making the engine think no grid exists and try to place one.
    cursor.execute(
        "SELECT order_id FROM bot_orders WHERE bot_id = ? AND order_type = 'grid'"
        " AND status IN ('open','new','placing') AND order_id NOT LIKE 'PLACING_%'",
        (bot_id,)
    )
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

    NOTE: does NOT call pass # conn.close() disabled for singleton safety — get_connection() is a thread-local singleton;
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


def clear_active_position_for_bot(bot_id: int, pair: str = None, cursor=None) -> None:
    """
    Remove the active_positions row(s) for this bot when it resets after TP/close.
    If cursor is provided, uses it directly (caller manages transaction).
    Otherwise, manages its own transaction.
    """
    try:
        if cursor:
            # Caller manages the transaction - just execute directly
            if pair:
                from engine.exchange_interface import normalize_symbol
                clean_pair = normalize_symbol(pair)
                cursor.execute("DELETE FROM active_positions WHERE bot_id = ? AND pair = ?", (bot_id, clean_pair))
            else:
                cursor.execute("DELETE FROM active_positions WHERE bot_id = ?", (bot_id,))
        else:
            conn = get_connection()
            conn.execute("BEGIN IMMEDIATE")
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
        if not cursor:
            try: conn.rollback()
            except: pass



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
        conn.execute("BEGIN IMMEDIATE")
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
    cursor.execute("SELECT b.id, b.name, b.pair, b.is_active, b.strategy_type, COALESCE(t.total_invested, 0), COALESCE(t.current_step, 0), b.last_error, b.last_error_time, b.status FROM bots b LEFT JOIN trades t ON b.id = t.bot_id")
    bots = cursor.fetchall()
    logger.debug(f"[GET_ALL_BOTS] Query returned {len(bots)} bots from DB.")
    return bots

def toggle_bot_active(bot_id, new_status):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if new_status:
            cursor.execute("UPDATE bots SET is_active = 1, status = 'Scanning' WHERE id = ?", (bot_id,))
        else:
            cursor.execute("UPDATE bots SET is_active = 0 WHERE id = ?", (bot_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to toggle bot {bot_id}: {e}")
        try: conn.rollback()
        except: pass

def update_bot_error(bot_id: int, error_msg: str):
    """Updates the last_error field for a bot. Set error_msg to None to clear."""
    try:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE bots SET last_error = ?, last_error_time = ? WHERE id = ?",
            (error_msg, int(time.time()) if error_msg else None, bot_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update bot error for {bot_id}: {e}")
        try: conn.rollback()
        except: pass

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
        pass # conn.close() disabled for singleton safety

def delete_bot(bot_id):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass
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
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_orders SET order_id = ?, status = 'open', updated_at = ? WHERE id = ?", (exchange_order_id, int(time.time()), db_id))
        conn.commit()
        return True
    except:
        try: conn.rollback()
        except: pass
        return False

def fail_order(db_id, reason):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        # 🚀 INTEGRITY GUARD: Never overwrite a physical fill with a 'failed' network status.
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'failed', notes = ?, updated_at = ? 
            WHERE id = ? 
              AND status NOT IN ('filled', 'partially_filled', 'open')
              AND COALESCE(filled_amount, 0) = 0
        """, (reason, int(time.time()), db_id))
        conn.commit()
        return True
    except:
        try: conn.rollback()
        except: pass
        return False

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
    This is the AUTHORITATIVE physical reality view for the monitor and reconciler.

    ══════════════════════════════════════════════════════════════════════
    RULE #1 — ONE-WAY MODE ACCOUNT (read this before touching this code)
    ══════════════════════════════════════════════════════════════════════
    This Binance account is configured in ONE-WAY MODE, not hedge mode.

    What that means on the exchange:
      - There is ONE net position per symbol (positive contracts = net LONG, negative = net SHORT).
      - Multiple bots can trade LONG and SHORT on the same symbol simultaneously.
        Their orders NET on the exchange (a LONG entry + a SHORT entry = net zero).
      - positionSide is ALWAYS 'BOTH' in raw API responses. It carries no directional info.
      - NEVER send positionSide in any order request — Binance returns -400 immediately.
      - Close orders must use reduceOnly=True + the correct side (sell=reduce long, buy=reduce short).

    Direction detection here:
      - In one-way mode p['side'] == 'both' always. Useless.
      - The SIGNED contracts field (positive/negative) is the only reliable direction signal.
      - amount > 0  → net LONG position  → side = 'LONG'
      - amount < 0  → net SHORT position → side = 'SHORT'

    INVARIANT: This table must always reflect exchange truth, not virtual ledger values.
    - All rows are replaced on every call (DELETE then INSERT).
    - Bot ownership (bot_id) assigned by lookup; unowned positions get bot_id=0.
    """
    global _EMPTY_SNAP_COUNTER
    conn = None
    try:
        conn = get_connection()

        if not positions or len(positions) == 0:
            current_count = conn.execute("SELECT COUNT(*) FROM active_positions").fetchone()[0]
            if current_count > 0:
                _EMPTY_SNAP_COUNTER += 1
                if _EMPTY_SNAP_COUNTER < _EMPTY_SNAP_THRESHOLD:
                    logger.warning(f"\u26a0\ufe0f [SAFETY-LATCH] Empty snapshot {_EMPTY_SNAP_COUNTER}/{_EMPTY_SNAP_THRESHOLD}. Keeping {current_count} existing positions.")
                    return
                else:
                    logger.info(f"\u2705 [SAFETY-LATCH] {_EMPTY_SNAP_COUNTER} consecutive empty snapshots. Clearing {current_count} stale positions.")
                    _EMPTY_SNAP_COUNTER = 0
        else:
            _EMPTY_SNAP_COUNTER = 0

        # ONE-WAY MODE direction detection:
        # p['side'] is always 'both' — useless. Use the SIGN of contracts.
        # amount > 0 → net LONG.  amount < 0 → net SHORT.
        # p.get('info', {}).get('positionSide') is always 'BOTH' — also useless.
        agg_positions = {}
        for p in positions:
            raw_symbol = p.get('symbol', 'UNKNOWN')
            from engine.exchange_interface import normalize_symbol
            symbol = normalize_symbol(raw_symbol)

            amount = float(p.get('contracts', 0) or p.get('size', 0) or 0)  # SIGNED
            entry_price = float(p.get('entryPrice', 0) or 0)
            if abs(amount) == 0:
                continue

            # Determine direction from signed amount (one-way mode only reliable method)
            side = 'LONG' if amount > 0 else 'SHORT'

            key = (symbol, side)
            if key not in agg_positions:
                agg_positions[key] = {'size': 0.0, 'value': 0.0}
            agg_positions[key]['size'] += abs(amount)
            agg_positions[key]['value'] += abs(amount) * abs(entry_price)


        # Full replacement: DELETE all, INSERT from exchange truth
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM active_positions")

        ts = int(time.time())
        orphan_count = 0
        owned_count = 0

        for (symbol, side), data in agg_positions.items():
            avg_price = data['value'] / data['size'] if data['size'] > 0 else 0
            owner_id = get_active_bot_id_by_symbol_direction(symbol, side) or 0
            conn.execute(
                "INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (owner_id, symbol, side, data['size'], avg_price, ts)
            )
            if owner_id == 0:
                orphan_count += 1
                logger.warning(f"\u26a0\ufe0f [REALITY-ORPHAN] {side} {symbol} qty={data['size']:.4f} @ {avg_price:.4f} \u2014 no owning bot (bot_id=0).")
            else:
                owned_count += 1
                logger.debug(f"[SNAP] Bot {owner_id} \u2192 {side} {symbol} qty={data['size']:.6f} @ {avg_price:.4f}")

        conn.commit()
        logger.info(f"\u2705 [SNAP] active_positions refreshed: {owned_count} owned + {orphan_count} orphans ({len(agg_positions)} total).")

    except Exception as e:
        logger.error(f"Failed to update active_positions snapshot: {e}")
        if conn:
            try:
                conn.rollback()
            except: pass

def update_trade_tp_price(bot_id: int, new_tp_price: float):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("UPDATE trades SET target_tp_price = ? WHERE bot_id = ?", (new_tp_price, bot_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update TP price for bot {bot_id}: {e}")
        try: conn.rollback()
        except: pass

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
    """Generates a unique tracking ID for a bot cycle."""
    import uuid
    return f"BPS_{uuid.uuid4().hex[:8]}"

def get_bot_position_id(bot_id):
    """Retrieves the current bot position ID from the trades table."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT bot_position_id FROM trades WHERE bot_id = ?', (bot_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Error fetching bot_position_id for bot {bot_id}: {e}")
        return None
    return str(uuid.uuid4())[:8].upper()

def close_bot_position(bot_id, close_type='MANUAL', close_price=0.0, close_pct=100.0, notes=''):
    """
    Closes or partially closes a bot's position in the database.
    """
    try:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        
        # Get current state
        cursor.execute("SELECT pair, direction, total_invested, avg_entry_price FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row:
            return {'success': False, 'error': 'Bot not found'}
        
        pair, direction, total_invested, avg_entry = row
        
        if close_pct >= 100:
            # Full close
            _reset_bot_after_tp_internal(cursor, bot_id, exit_price=close_price, direction=direction, action_label=close_type, notes=notes)
            conn.commit()
            return {'success': True, 'status': 'Fully Closed'}
        else:
            # Partial close - just reduce total_invested and log
            reduction = total_invested * (close_pct / 100.0)
            new_invested = max(0, total_invested - reduction)
            
            cursor.execute("UPDATE trades SET total_invested = ? WHERE bot_id = ?", (new_invested, bot_id))
            _log_trade_internal(cursor, bot_id, f'PARTIAL_{close_type}', pair, close_price, reduction / close_price if close_price > 0 else 0, reduction, notes=notes)
            conn.commit()
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
                        basket_start_time = ?,
                        entry_confirmed = 1,
                        cycle_phase = 'ACTIVE'
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
                    int(time.time()),
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
                        basket_start_time = ?,
                        entry_confirmed = 1,
                        cycle_phase = 'ACTIVE'
                    WHERE bot_id = ?
                """, (
                    added_invested,
                    avg_price,
                    avg_price,
                    added_invested, added_invested, avg_price,
                    avg_price,
                    force_step, safe_step,           # ROOT CAUSE FIX: Reconciler can force alignment
                    not step_is_none, safe_step, safe_step,
                    int(time.time()),
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
        try: conn.rollback()
        except: pass
        logger.error(f"❌ [ATOMIC-FAIL] Failed to accumulate fill for bot {bot_id}: {e}")
        raise

def set_trade_from_ledger(bot_id: int, total_invested: float, avg_price: float, current_step: int):
    """
    Atomically overwrites a trade state with exact mathematical truth from the ledger.
    NEVER ADDS. This safely breaks any double-counting loops by forcing absolute state.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM trades WHERE bot_id = ?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("""
                UPDATE trades 
                SET total_invested = ?, avg_entry_price = ?, current_step = ?, entry_confirmed = 1 
                WHERE bot_id = ?
            """, (total_invested, avg_price, current_step, bot_id))
        else:
            cursor.execute("""
                INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time, cycle_id)
                VALUES (?, ?, ?, ?, 1, ?, COALESCE((SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id=?), 1))
            """, (bot_id, current_step, total_invested, avg_price, int(time.time()), bot_id))
            
        cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
        conn.commit()
        logger.info(f"✅ [ATOMIC-SET] Bot {bot_id} overwritten from ledger proof: Inv=${total_invested:.2f} @ {avg_price:.4f} (Step {current_step})")
    except Exception as e:
        try: conn.rollback()
        except: pass
        logger.error(f"❌ [ATOMIC-SET-FAIL] Failed to force-set trade state for bot {bot_id}: {e}")
        raise

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
            cursor.execute("SELECT pair, direction, normalized_pair FROM bots WHERE id = ?", (bid,))
            row = cursor.fetchone()
            if not row:
                continue
            bpair, bdir, bnorm = row
            if (bnorm or normalize_symbol(bpair)).upper() == norm_symbol and bdir.strip().upper() == norm_direction:
                # Confirmed: this bot placed a real order for this symbol AND matches the position direction.
                return bid

        # --- FALLBACK: Direction + Active-Position check ---
        # v2.2: Accept ownership via EITHER total_invested > 0 OR open_qty > 0.
        # Root cause of BNB/XAU orphan: the original query required total_invested > 0.
        # After a reset that zeroed total_invested but left open_qty intact (e.g. from
        # the integrity bootstrap or a bypassed sync order), the bot was invisible here
        # and the snapshot assigned bot_id=0 (orphan) causing false REALITY-ORPHAN logs,
        # grace-period stalls, and the Orphan panel showing in the UI.
        # FIX: include open_qty > 0 as an equivalent ownership signal.
        cursor.execute("""
            SELECT b.id, b.pair, b.direction
            FROM bots b
            JOIN trades t ON b.id = t.bot_id
            WHERE b.is_active = 1
              AND (t.total_invested > 0 OR t.open_qty > 0)
        """)
        for bid, bpair, bdir in cursor.fetchall():
            if (normalize_symbol(bpair).upper() == norm_symbol
                    and bdir.strip().upper() == norm_direction):
                return bid

        # --- FIX 2: POST-TP DRAIN GUARD ---
        # If no bot has invested > 0, residual physical positions currently get orphaned to bot_id=0.
        # We must check if any bot recently closed a TP on this pair-direction within the last 5 minutes.
        # If so, this residual belongs to them.
        import time # ensure time is imported
        five_mins_ago = int(time.time()) - 300
        cursor.execute("""
            SELECT b.id, b.pair, b.direction 
            FROM bots b
            JOIN bot_orders o ON b.id = o.bot_id
            WHERE b.is_active = 1
              AND o.order_type = 'tp'
              AND o.status IN ('filled', 'closed', 'reset_cleared')
              AND o.updated_at >= ?
            ORDER BY o.updated_at DESC
        """, (five_mins_ago,))
        
        for bid, bpair, bdir in cursor.fetchall():
            if (normalize_symbol(bpair).upper() == norm_symbol
                    and bdir.strip().upper() == norm_direction):
                logger.info(f"🛡️ [POST-TP GUARD] Assigned residual {symbol} ({direction}) to Bot {bid} (Recent TP).")
                return bid

        return None
    except Exception as e:
        logger.error(f"Error in get_active_bot_id_by_symbol_direction: {e}")
        return None
    # NOTE: No pass # conn.close() disabled for singleton safety — get_connection() returns a thread-local persistent connection.


def _calculate_formula_step(bot_id: int, total_cost: float, fallback_step: int, cursor, cycle_id: int) -> int:
    """
    Validates and corrects the current_step using the ABSOLUTE LEDGER SUCCESSION PROOF:
    Truth Hierarchy:
    1. PROOF (Ledger): Count highest verified 'filled' step in bot_orders for this cycle.
    2. SANITY (Fallback): If no ledger fills exist, Step is 0.

    Mathematical derivation is strictly for cross-referencing in logs/UI and MUST NOT
    automatically move the bot's current_step, as that leads to 'Step Inflation' (fixing drift).
    """
    try:
        import math as _math
        import logging
        logger = logging.getLogger(__name__)

        # 🚀 1. ABSOLUTE SUCCESSION PROOF: Highest milestone reached with >= 99% fill ratio.
        orders = cursor.execute(
            """SELECT step, amount, filled_amount FROM bot_orders
               WHERE bot_id=? AND cycle_id=?
               AND filled_amount > 0
               AND status IN ('filled', 'closed', 'canceled', 'cancelled', 'partially_filled')
               ORDER BY step DESC""",
            (bot_id, cycle_id)
        ).fetchall()

        ledger_step = 0
        for o_step, o_amount, o_filled in orders:
            if o_step is None: continue

            target = float(o_amount or 0)
            filled = float(o_filled or 0)
            
            if target > 0 and (filled / target) >= 0.99:
                ledger_step = int(o_step)
                break # Found highest completed step
            elif target == 0 and filled > 0:
                # Edge case: adoptions/carries with 0 target but positive fill
                ledger_step = max(ledger_step, int(o_step))

        # 🚀 2. THE IDENTITY GUARD
        if ledger_step != fallback_step:
            logger.info(f"📐 [SUCCESSION-PROOF] Bot {bot_id}: Milestone Ledger confirms Step {ledger_step} (Previously {fallback_step}).")
        
        return ledger_step
    except Exception as e:
        logger.error(f"Error in _calculate_formula_step (bot {bot_id}): {e}")
    return fallback_step


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
        # Resolve current cycle_id and side from trades table
        row = cursor.execute(
            "SELECT COALESCE(cycle_id, 1), COALESCE(position_side, 'LONG') FROM trades WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if not row:
            return 0.0, 0.0, 0.0, 0
        cycle_id, bot_side = row

        # ... (Healing block omitted for brevity)

        # 🚀 HEDGE-MODE FIX: Filter fills by the bot's position_side.
        # This prevents a LONG bot from adopting SHORT fills (and vice versa).
        # Find the wipe wall: the highest row ID of any reset_cleared/auto_closed marker
        # in this cycle. Rows at id <= wall_id were definitively archived at that point
        # and must stay dead, even if they have filled_amount > 0 (historical ghosts).
        # Rows with id > wall_id genuinely belong to the current active cycle.
        # This is more reliable than basket_start_time (which is continuously updated
        # as new orders are placed and can postdate legitimate fills within this cycle).
        wipe_wall_row = cursor.execute("""
            SELECT COALESCE(MAX(id), 0)
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id = ?
              AND status IN ('reset_cleared', 'auto_closed')
        """, (bot_id, cycle_id)).fetchone()
        wipe_wall_id = int(wipe_wall_row[0]) if wipe_wall_row else 0

        cursor.execute("""
            SELECT
                ROUND(COALESCE(SUM(
                    CASE WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN (bo.filled_amount * bo.price) ELSE 0.0 END
                ), 0.0), 8) AS bought_cost,
                ROUND(COALESCE(SUM(
                    CASE WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount ELSE 0.0 END
                ), 0.0), 8) AS bought_qty,
                ROUND(COALESCE(SUM(
                    CASE WHEN bo.order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN bo.filled_amount ELSE 0.0 END
                ), 0.0), 8) AS sold_qty,
                COALESCE(MAX(bo.step), 0) AS max_step
            FROM bot_orders bo
            WHERE bo.bot_id  = ?
              AND bo.cycle_id = ?
              AND (bo.position_side = ? OR bo.position_side IS NULL OR bo.position_side = 'BOTH' OR bo.position_side = '')  -- 🚀 FUNDAMENTAL FIX: NULL-tolerant Side filter (v2.1)
              AND bo.filled_amount > 0
              AND bo.price > 0
              AND bo.client_order_id LIKE 'CQB_%'
              -- 🚀 WIPE-WALL INTEGRITY GUARD:
              -- Hard exclude all structurally archived rows (they are history).
              AND bo.status NOT IN ('auto_closed', 'reset_cleared')
              -- Exclude rows at or behind the wipe wall — they belong to a previous
              -- sub-cycle that was explicitly closed. 0 means no wall (fresh cycle).
              AND (? = 0 OR bo.id > ?)
              -- For 'failed' rows: only include if they have genuine fills AND sit
              -- above the wipe wall (i.e., they are true REST-timeout races on
              -- WebSocket-confirmed fills, not ancient pre-wipe ghosts).
              AND (
                  bo.status NOT IN ('placing', 'failed')
                  OR (bo.status = 'failed' AND bo.filled_amount > 0)
              )
        """, (bot_id, cycle_id, bot_side, wipe_wall_id, wipe_wall_id))

        r = cursor.fetchone()
        bought_cost = float(r[0] or 0.0)
        bought_qty  = float(r[1] or 0.0)
        sold_qty    = float(r[2] or 0.0)
        max_step    = int(r[3] or 0)
        # Snap sub-epsilon fractions to zero — prevents invisible IEEE 754 dust
        # from triggering spurious CARRY-OVER rows on the next cycle.
        if abs(float(r[0] or 0.0)) <= 1e-8:
            return 0.0, 0.0, 0.0, 0

        net_qty_raw = float(r[1] or 0.0) - float(r[2] or 0.0)
        total_qty = round(net_qty_raw, 8)  # Normalized net position quantity

        if total_qty > 1e-8:
            avg_price = bought_cost / bought_qty if bought_qty > 1e-8 else 0.0
            total_cost = total_qty * avg_price
            if max_step == 0:
                max_step = 1

            # ✅ MARTINGALE CROSS-REFERENCE: Validate/correct max_step using the
            # inverse geometric series formula.  Adoption rows may have been written
            # with step=1 even when total_invested covers multiple martingale steps.
            # This catches those stale rows every sync cycle without re-scanning orders.
            if total_cost > 0:
                max_step = _calculate_formula_step(bot_id, total_cost, max_step, cursor, cycle_id)
            return total_cost, avg_price, total_qty, max_step

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
              AND status NOT IN ('open', 'new', 'placing', 'failed', 'auto_closed', 'reset_cleared')
        """, (bot_id, cycle_id)).fetchone()
        carry_qty = float(carry_row[0] or 0.0)

        if carry_qty <= 1e-8:
            return 0.0, 0.0, 0.0, 0  # Truly no position this cycle

        # Compute avg price for carry position
        # Priority: (1) bot_orders CARRY fills directly, (2) active_positions, (3) trades cache
        bot_row = cursor.execute(
            "SELECT pair, direction FROM bots WHERE id = ?", (bot_id,)
        ).fetchone()
        if not bot_row:
            return 0.0, 0.0, 0.0, 0

        pair, direction = bot_row
        norm_pair = pair.split(':')[0].replace('/', '')
        snap_side = 'LONG' if str(direction).upper() == 'LONG' else 'SHORT'

        # ── SOURCE 1: Compute directly from CARRY bot_orders fills (most accurate) ──
        # Works at startup BEFORE active_positions is populated.
        carry_price_row = cursor.execute("""
            SELECT
                SUM(filled_amount * price) / NULLIF(SUM(filled_amount), 0)
            FROM bot_orders
            WHERE bot_id = ?
              AND cycle_id = ?
              AND client_order_id LIKE 'CQB_%'
              AND client_order_id LIKE '%_CARRY_%'
              AND filled_amount > 0
              AND price > 0
              AND status NOT IN ('open', 'new', 'placing', 'failed', 'auto_closed', 'reset_cleared')
        """, (bot_id, cycle_id)).fetchone()

        if carry_price_row and carry_price_row[0] and float(carry_price_row[0]) > 0:
            carry_avg_price = float(carry_price_row[0])
            carry_cost = carry_qty * carry_avg_price
            logger.info(
                f"[RECOMPUTE-CARRY] Bot {bot_id} cycle {cycle_id}: "
                f"CARRY qty={carry_qty:.8f} @ avg={carry_avg_price:.4f} "
                f"(from bot_orders CARRY fills). total_invested={carry_cost:.4f}"
            )
            carry_step = _calculate_formula_step(bot_id, carry_cost, 1, cursor, cycle_id)
            return carry_cost, carry_avg_price, carry_qty, carry_step

        # ── SOURCE 2: active_positions snapshot (available after prime_startup_snapshot) ──
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
            carry_step = _calculate_formula_step(bot_id, carry_cost, 1, cursor, cycle_id)
            return carry_cost, carry_avg_price, carry_qty, carry_step

        # ── SOURCE 3: Last resort — cached avg from trades row ──
        cached_avg = cursor.execute(
            "SELECT avg_entry_price FROM trades WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if cached_avg and float(cached_avg[0] or 0) > 0:
            carry_avg_price = float(cached_avg[0])
            carry_cost = carry_qty * carry_avg_price
            logger.warning(
                f"[RECOMPUTE-CARRY] Bot {bot_id}: CARRY fallback to trades.avg_entry_price={carry_avg_price:.4f} "
                f"(no CARRY fills or active_positions). qty={carry_qty:.8f}, cost={carry_cost:.4f}"
            )
            carry_step = _calculate_formula_step(bot_id, carry_cost, 1, cursor, cycle_id)
            return carry_cost, carry_avg_price, carry_qty, carry_step

        logger.warning(
            f"[RECOMPUTE-CARRY] Bot {bot_id}: CARRY qty={carry_qty:.8f} — "
            f"no CARRY fills, no active_positions, no cached avg. Cannot price position."
        )
        return 0.0, 0.0, 0.0, 0

    except Exception as e:
        logger.error(f"Error in recompute_invested_from_orders (bot {bot_id}): {e}")
        return 0.0, 0.0, 0.0, 0


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
        recomputed_cost, recomputed_avg, recomputed_qty, recomputed_step = recompute_invested_from_orders(bot_id)

        conn = get_connection()
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        if not row:
            return False

        cached_invested, cached_avg, cached_step = float(row[0] or 0), float(row[1] or 0), int(row[2] or 0)

        # Derive quantity from the cached trades table source for comparison
        cached_qty = (cached_invested / cached_avg) if cached_avg > 0 else 0.0

        delta_qty = abs(recomputed_qty - cached_qty)

        # 🚀 THE ABSOLUTE GROUND TRUTH [V1.8.6]
        # Recomputed fills are authoritative. If the ledger is empty, the bot is IDLE.
        # We do NOT allow 'Math Proof' to override an empty ledger here because that 
        # is how 'Step Inflation' happens (by trusting legacy total_invested).
        
        if recomputed_qty <= QTY_EPSILON:
            # Ledger is empty. Ensure trades row is cleared.
            if cached_invested > 0 or cached_step > 0:
                logger.warning(f"🧹 [DNA-WIPE] Bot {bot_id}: No ledger fills found. Resetting phantom state (was ${cached_invested:.2f}, Step {cached_step}).")
                # 🚀 ROOT CAUSE FIX (v2.1.1): Do NOT clear basket_start_time here.
                # Clearing BST to 0 caused a silent oscillation with the offline DNA-guard:
                #   DNA-WIPE clears BST → next pass: bot_start=0 → old 1h cutoff drops fills
                #   → recompute still returns 0 → DNA-WIPE fires again → permanent deadlock.
                # BST is an EE-timer (engine-operation timestamp), not a cycle boundary.
                # It is safe to preserve it; CST (cycle_start_time) is the authoritative boundary.
                # BST will be refreshed naturally by seal_trade_state when the bot re-enters.
                cursor.execute("""
                    UPDATE trades 
                    SET total_invested = 0, avg_entry_price = 0, current_step = 0, 
                        entry_confirmed = 0
                    WHERE bot_id = ?
                """, (bot_id,))
                conn.commit()
                return True
            return False

        if delta_qty <= QTY_EPSILON and recomputed_step == cached_step:
            conn.commit()
            return False

        # 🚀 CYCLE-CONTAMINATION GUARD [V1.7.3]:
        # If recomputed_cost is NEGATIVE, it means historical entries and exits
        # across many sub-cycles all share the same cycle_id and exits dominate.
        # This is NOT a real position change — it's ledger pollution.
        # A negative total_invested is physically impossible (can't short by accident).
        # Skip the overwrite to preserve any physical imprint written by the reconciler.
        if recomputed_cost < 0:
            logger.warning(
                f"⚠️ [LEDGER-SYNC] Bot {bot_id}: Recomputed cost is NEGATIVE "
                f"({recomputed_cost:.4f}) — cycle contamination detected. "
                f"Skipping overwrite to preserve physical imprint."
            )
            conn.commit()
            return False

        # Discrepancy detected — recomputed fills are authoritative
        logger.warning(
            f"🔧 [LEDGER-SYNC] Bot {bot_id}: qty drift detected. "
            f"Cached={cached_qty:.8f} vs Confirmed={recomputed_qty:.8f} (Δ={delta_qty:.8f}). "
            f"Correcting trades row from order fills."
        )

        # 🚀 EE TIMER RESET [V1.9.0]
        # If the quantity is increasing significantly (a safety order/grid hit), 
        # we reset the basket_start_time to 'now'. This restarts the Early Exit 
        # countdown for the new, averaged position.
        new_basket_time_sql = ""
        params = [round(recomputed_cost, 8), round(recomputed_avg, 8), recomputed_step, recomputed_cost]
        
        # Reset if step increases or qty increases by more than 1% (avoid noise resets)
        if recomputed_step > cached_step or recomputed_qty > (cached_qty * 1.01):
            logger.info(f"⏳ [EE-RESET] Bot {bot_id}: Grid hit detected. Resetting basket timer to now.")
            new_basket_time_sql = ", basket_start_time = ?"
            params.append(int(time.time()))
        
        params.append(bot_id)

        sql = f"""
            UPDATE trades
            SET total_invested  = ?,
                avg_entry_price = ?,
                current_step    = ?,
                entry_confirmed = CASE WHEN ? > 0 THEN 1 ELSE entry_confirmed END
                {new_basket_time_sql}
            WHERE bot_id = ?
        """
        cursor.execute(sql, params)
        conn.commit()
        return True

    except Exception as e:
        try: conn.rollback()
        except: pass
        logger.error(f"Error in sync_trades_from_orders (bot {bot_id}): {e}")
        return False


    cursor.execute('SELECT bot_position_id FROM trades WHERE bot_id = ?', (bot_id,))
    res = cursor.fetchone()
    return res[0] if res else None

def reconcile_with_db(bot_id, current_price, open_orders, exchange_position):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT t.total_invested, b.pair, b.direction FROM trades t JOIN bots b ON t.bot_id = b.id WHERE t.bot_id = ?", (bot_id,))
    res = cursor.fetchone()
    if not res: return {'success': False}
    total_invested, pair, direction = res

    if not exchange_position or float(exchange_position.get('size', 0)) == 0:
        if total_invested > 0:
            # ============================================================
            # 🚀 PHYSICAL-IMPRINT GUARD v1.8.6
            # Before zeroing the ledger, cross-check TWO sources of truth:
            #
            # SOURCE A — active_positions table (persistent exchange snapshot):
            #   If a row exists for this bot with size > 0, the exchange still
            #   physically holds this position. The exchange_position argument
            #   may be stale (REST lag, wrong symbol format, etc).
            #
            # SOURCE B — recompute_invested_from_orders (bot_orders fills):
            #   If confirmed fill records exist above the wipe wall, a real
            #   physical position was opened and not yet closed by the system.
            #
            # If EITHER source says the position is still open, ABORT the wipe.
            # Only wipe when BOTH agree the position is truly flat.
            # ============================================================

            # Source A: check active_positions for a live physical imprint
            norm_pair = pair.split(':')[0].replace('/', '')
            ap_side = 'LONG' if str(direction).upper() == 'LONG' else 'SHORT'
            ap_row = cursor.execute(
                "SELECT size FROM active_positions WHERE bot_id = ? AND size > 0",
                (bot_id,)
            ).fetchone()
            if ap_row and float(ap_row[0]) > 0:
                logger.warning(
                    f"🛡️ [RECONCILE-GUARD] Bot {bot_id}: exchange_position=None but "
                    f"active_positions shows size={ap_row[0]:.6f}. "
                    f"Skipping ledger wipe — physical imprint still present."
                )
                return {'success': True}  # Position is live — do NOT wipe

            # Source B: check bot_orders ground truth
            recomputed_cost, recomputed_avg, recomputed_qty, _ = recompute_invested_from_orders(bot_id)
            if recomputed_qty > 1e-6:
                logger.warning(
                    f"🛡️ [RECONCILE-GUARD] Bot {bot_id}: exchange_position=None but "
                    f"recompute confirms qty={recomputed_qty:.6f} still filled. "
                    f"Skipping ledger wipe — fill records intact."
                )
                return {'success': True}  # bot_orders confirms fills — do NOT wipe

            # Both sources agree: position is truly flat. Safe to wipe.
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,))
                res_cyc = cursor.fetchone()
                old_cycle = res_cyc[0] if res_cyc and res_cyc[0] else 1
                new_cycle = old_cycle + 1
                cursor.execute("UPDATE trades SET current_step = 0, total_invested = 0, avg_entry_price = 0, target_tp_price = 0, last_exit_price = ?, last_exit_time = ?, basket_start_time = ?, entry_confirmed = 0, entry_order_id = NULL, tp_order_id = NULL, bot_position_id = NULL, close_type = 'RECONCILE', cycle_id = ? WHERE bot_id = ?", (current_price, int(time.time()), int(time.time()), new_cycle, bot_id))
                cursor.execute("UPDATE bot_orders SET status = 'auto_closed', updated_at = ? WHERE bot_id = ? AND status = 'open'", (int(time.time()), bot_id))
                cursor.execute("UPDATE bot_orders SET status = 'reset_cleared', updated_at = ? WHERE bot_id = ? AND status NOT IN ('open', 'new', 'auto_closed', 'reset_cleared') AND order_type != 'hedge'", (int(time.time()), bot_id))
                cursor.execute("UPDATE bots SET status='Scanning' WHERE id = ?", (bot_id,))
                conn.commit()
                logger.info(f"[RECONCILE] Bot {bot_id}: ledger zeroed (cycle {old_cycle} → {new_cycle}). Both AP and recompute confirmed flat.")
            except Exception as e:
                logger.error(f"Reconcile DB update failed: {e}")
                try: conn.rollback()
                except: pass
    return {'success': True}

# 🛡️ MANUAL WHITELIST HELPERS (V2.1.0)
def add_manual_whitelist(pair: str, side: str, qty: float):
    """Register a manual position that the bot should ignore during reconciliation."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        # Side must be normalized to 'LONG' or 'SHORT'
        norm_side = side.upper()
        # Side check
        if norm_side not in ['LONG', 'SHORT']:
            logger.error(f"Invalid side for manual whitelist: {side}")
            return

        # Simple delete-then-insert for clean state per pair/side
        cursor.execute("DELETE FROM manual_whitelists WHERE pair = ? AND side = ?", (pair, norm_side))
        cursor.execute("""
            INSERT INTO manual_whitelists (pair, side, qty, created_at)
            VALUES (?, ?, ?, ?)
        """, (pair, norm_side, qty, int(time.time())))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to add manual whitelist for {pair}: {e}")

def get_manual_whitelists(pair: str = None) -> List[Dict]:
    """Retrieve all active manual whitelists, optionally filtered by pair."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if pair:
            cursor.execute("SELECT pair, side, qty, created_at FROM manual_whitelists WHERE pair = ?", (pair,))
        else:
            cursor.execute("SELECT pair, side, qty, created_at FROM manual_whitelists")
        rows = cursor.fetchall()
        return [{'pair': r[0], 'side': r[1], 'qty': float(r[2]), 'created_at': r[3]} for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch manual whitelists: {e}")
        return []

def remove_manual_whitelist(pair: str, side: str):
    """Remove a specific manual whitelist."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM manual_whitelists WHERE pair = ? AND side = ?", (pair, side.upper()))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to remove manual whitelist for {pair}: {e}")

def clear_manual_whitelists_for_pair(pair: str):
    """Remove all manual whitelists for a pair (used when position is flattened)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM manual_whitelists WHERE pair = ?", (pair,))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to clear manual whitelists for {pair}: {e}")

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
    """Public wrapper for log_trade with transaction management."""
    try:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        _log_trade_internal(cursor, bot_id, action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl)
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to log trade: {e}")
        try: conn.rollback()
        except: pass
        return False

def _log_trade_internal(cursor, bot_id, action, symbol, price, amount, cost_usdc, order_id="UNKNOWN", step=0, notes="", pnl=0.0, position_side=None):
    """Internal implementation that assumes an active transaction/cursor."""
    # 🚀 FUNDAMENTAL FIX: side-locking (Prevent Crossover)
    cursor.execute("SELECT direction FROM bots WHERE id=?", (bot_id,))
    bot_row = cursor.fetchone()
    bot_direction = bot_row[0].upper() if bot_row else 'LONG'
    
    # In Hedge Mode, we MUST strictly match. 'BOTH' is treated as a mismatch for directional bots.
    resolved_side = str(position_side).upper() if position_side else bot_direction
    if resolved_side != bot_direction:
        logger.error(f"🛡️ [SIDE-LOCK REJECT] Attempted to log {resolved_side} trade for {bot_direction} Bot {bot_id} ({symbol}). Trade ignored.")
        return False

    # Robust conversion for incorrect argument types from bot_executor
    if isinstance(notes, (int, float)): notes = str(notes)
    if isinstance(pnl, str): 
        try: pnl = float(pnl)
        except: pnl = 0.0
        
    cursor.execute("""
        INSERT INTO trade_history (bot_id, timestamp, action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl, position_side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, int(time.time()), action, symbol, price, amount, cost_usdc, order_id, step, notes, pnl, bot_direction))
    return True

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
            
        conn.execute("BEGIN IMMEDIATE")
        cursor.execute(sql, tuple(params))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update order status {order_id}: {e}")
        try: conn.rollback()
        except: pass

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
        try: conn.rollback()
        except: pass

def save_bot_order(bot_id, order_type, exchange_order_id, price, amount, step, status='open', client_order_id=None, notes=None, position_side=None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Resolve current cycle_id and bot direction
        cursor.execute("SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id=?", (bot_id,))
        c_row = cursor.fetchone()
        cycle_id = c_row[0] if c_row else 1
        
        cursor.execute("SELECT direction FROM bots WHERE id=?", (bot_id,))
        b_row = cursor.fetchone()
        bot_direction = b_row[0].upper() if b_row else 'LONG'

        # 🚀 FUNDAMENTAL FIX: side-locking (One-Way Mode tolerant)
        # If a position_side is provided (e.g., from adoption or API), it MUST match bot direction.
        # We allow 'BOTH' and NULL/Empty as they represent One-Way mode contributing to our side.
        upper_side = str(position_side or 'BOTH').upper()
        if upper_side not in (bot_direction, 'BOTH', 'NONE', ''):
            logger.error(f"🛡️ [SIDE-LOCK REJECT] Attempted to save {position_side} order for {bot_direction} Bot {bot_id}. Cross-side crossover blocked.")
            return None

        # 🚀 FUNDAMENTAL FIX: If the exchange immediately returns 'closed' or 'filled' on placement
        # (e.g. crossing the spread as a taker), we must stamp filled_amount immediately so 
        # the net_qty math in reset_bot_after_tp is balanced, even if websockets/reconciler misses it.
        initial_fill = amount if status.lower() in ('filled', 'closed') else 0.0

        # 🛡️ CID DEDUP GUARD (v2.1.2): Prevent WS+REST dual-fire race from inflating the ledger.
        # When both the WebSocket fill event and the REST reconciler call save_bot_order for the
        # same logical order (same client_order_id), each path receives a different native exchange
        # order_id, bypassing the old order_id UNIQUE constraint and creating phantom duplicate rows.
        # SOLUTION: If a non-cancelled row already exists for this (bot_id, client_order_id, cycle_id),
        # UPDATE it in place (stamping the new exchange order_id) instead of inserting a new row.
        # This is safe: we always prefer the most recent exchange order_id (REST confirmation > WS).
        if client_order_id:
            cursor.execute("""
                SELECT id, status, filled_amount FROM bot_orders
                WHERE bot_id=? AND client_order_id=? AND cycle_id=?
                  AND status NOT IN ('cancelled','canceled','failed','reset_cleared','auto_closed')
                ORDER BY id DESC LIMIT 1
            """, (bot_id, client_order_id, cycle_id))
            existing_cid_row = cursor.fetchone()
            if existing_cid_row:
                ex_row_id, ex_status, ex_filled = existing_cid_row
                # Only upgrade status/filled — never downgrade a 'filled' row back to 'open'
                final_status = status
                if ex_status in ('filled', 'closed') and status not in ('filled', 'closed'):
                    final_status = ex_status
                final_fill = max(float(ex_filled or 0), initial_fill)
                cursor.execute("""
                    UPDATE bot_orders
                    SET order_id=?, price=?, amount=?, filled_amount=?, status=?, updated_at=?
                    WHERE id=?
                """, (exchange_order_id, price, amount, final_fill, final_status, int(time.time()), ex_row_id))
                logger.debug(
                    f"[CID-DEDUP] Bot {bot_id} {order_type} cid={client_order_id}: "
                    f"updated existing row (id={ex_row_id}) instead of inserting duplicate."
                )
                # Still sync trades.*_order_id pointer to the latest exchange order_id
                if order_type == 'entry':
                    cursor.execute("UPDATE trades SET entry_order_id=? WHERE bot_id=?", (exchange_order_id, bot_id))
                elif order_type == 'tp':
                    cursor.execute("UPDATE trades SET tp_order_id=? WHERE bot_id=?", (exchange_order_id, bot_id))
                conn.commit()
                return ex_row_id

        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, notes, cycle_id, position_side) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bot_id, step, order_type, exchange_order_id, price, amount, initial_fill, status, int(time.time()), client_order_id, notes, cycle_id, bot_direction))
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
        try: conn.rollback()
        except: pass
        return None

# NOTE: update_order_status is defined earlier (L1393) — correct version with updated_at timestamp.

def update_bot_order_exchange_id(db_row_id, exchange_order_id, status='open'):
    """
    🚀 PRE-COMMIT PATTERN: After placing an order on the exchange, call this to stamp the real
    exchange order_id onto the 'placing' row that was pre-committed before the exchange call.
    If exchange_order_id is None (order placement failed), marks the row as 'failed'.

    🚀 BUG FIX: Also back-fills trades.tp_order_id / trades.entry_order_id when stamping a real
    exchange ID. Previously the pre-commit pattern wrote 'PLACING_{clientOrderId}' to trades
    (via save_bot_order), and this function only updated bot_orders — leaving trades permanently
    stuck with the placeholder string, causing the stalemate evictor to query an invalid ID.
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
            # 🚀 BACK-FILL TRADES: Resolve order_type and bot_id from this row, then update trades.
            row = conn.execute(
                "SELECT bot_id, order_type FROM bot_orders WHERE id=?", (db_row_id,)
            ).fetchone()
            if row:
                bot_id_for_row, order_type_for_row = row
                if order_type_for_row == 'tp':
                    # Only update if trades still has the old PLACING_ value (or was not set yet)
                    conn.execute(
                        "UPDATE trades SET tp_order_id=? WHERE bot_id=?"
                        " AND (tp_order_id IS NULL OR tp_order_id LIKE 'PLACING_%')",
                        (exchange_order_id, bot_id_for_row)
                    )
                elif order_type_for_row == 'entry':
                    conn.execute(
                        "UPDATE trades SET entry_order_id=? WHERE bot_id=?"
                        " AND (entry_order_id IS NULL OR entry_order_id LIKE 'PLACING_%')",
                        (exchange_order_id, bot_id_for_row)
                    )
        else:
            # 🚀 INTEGRITY GUARD: A REST API timeout returning None MUST NOT overwrite a 
            # WebSocket fill that arrived faster asynchronously.
            conn.execute(
                """
                UPDATE bot_orders 
                SET status='failed', updated_at=? 
                WHERE id=? 
                  AND status NOT IN ('filled', 'partially_filled', 'open')
                  AND COALESCE(filled_amount, 0) = 0
                """,
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
    from engine.exchange_interface import normalize_symbol
    norm_pair = normalize_symbol(pair)
    cursor.execute("""
        SELECT t.*, b.name, b.direction
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE (b.pair = ? OR b.normalized_pair = ?) AND t.total_invested > 0 AND b.is_active = 1
    """, (pair, norm_pair))
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
        cursor = conn.cursor()
        
        # 1. Update Virtual Positions
        for update in trade_updates:
            bot_id = update['bot_id']
            # Using UPSERT logic for robustness
            cursor.execute("""
                INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, basket_start_time, current_step)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    total_invested = excluded.total_invested,
                    avg_entry_price = excluded.avg_entry_price,
                    entry_confirmed = excluded.entry_confirmed,
                    current_step = excluded.current_step
                WHERE bot_id = ? AND (current_step <= excluded.current_step OR excluded.current_step = 0)
            """, (bot_id, update['total_invested'], update['avg_entry_price'], update['entry_confirmed'], update['basket_start_time'], update.get('current_step', 0), bot_id))

        # 2. Update Physical Positions
        cursor.execute("DELETE FROM active_positions")
        
        written_count = 0
        for p in physical_positions:
            raw_symbol = p.get('symbol', 'UNKNOWN')
            from engine.exchange_interface import normalize_symbol
            symbol = normalize_symbol(raw_symbol)
            amount = float(p.get('contracts', 0) or p.get('amount', 0) or p.get('size', 0) or 0)
            p_side = p.get('side', '').lower()
            if p_side == 'short':
                side = 'SHORT'
            elif p_side == 'long':
                side = 'LONG'
            else:
                side = 'LONG' if amount > 0 else 'SHORT'
                
            entry_price = float(p.get('entryPrice', 0) or 0)
            
            if amount != 0:
                # --- START BRIDGE REFACTOR (Phase 3) ---
                cursor.execute("SELECT id FROM bots WHERE normalized_pair = ? AND direction = ? AND is_active = 1 LIMIT 1", (symbol, side))
                row = cursor.fetchone()
                owner_id = row[0] if row else 0
                
                if owner_id == 0:
                    # Fallback to the more complex client_order_id proof logic
                    owner_id = get_active_bot_id_by_symbol_direction(symbol, side) or 0
                
                # Defensive Logging for bridge misses
                if owner_id == 0:
                     logger.warning(f"⚠️ [BRIDGE-MISS] No bot owner found for {symbol} {side} (Qty: {amount})")

                # --- END BRIDGE REFACTOR ---
                
                try:
                    cursor.execute("""
                        INSERT OR REPLACE INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (owner_id, symbol, side, abs(amount), entry_price, int(time.time())))
                    written_count += 1
                except Exception as insert_err:
                    logger.error(f"❌ INSERT FAILED for {symbol}: {insert_err}")
        
        conn.commit()
        if len(physical_positions) > 0:
            logger.info(f"✅ Snapshot sync complete: {written_count}/{len(physical_positions)} positions mapped to bots.")
        
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
                pass # conn.close() disabled for singleton safety
            except: pass


# 🛡️ ARCHITECT'S SHIELD: Manual Whitelist Helpers

def add_manual_whitelist(pair: str, side: str, qty: float):
    """Adds a manual quantity to the whitelist to be ignored by reconciler."""
    try:
        conn = get_connection()
        c = conn.cursor()
        # Check if already exists for this exact pair/side - if so, update QTY (overwrite)
        c.execute("SELECT id FROM manual_whitelists WHERE pair=? AND side=?", (pair, side.upper()))
        existing = c.fetchone()
        
        if existing:
            c.execute("UPDATE manual_whitelists SET qty=? WHERE id=?", (qty, existing[0]))
        else:
            c.execute("INSERT INTO manual_whitelists (pair, side, qty) VALUES (?, ?, ?)", (pair, side.upper(), qty))
        
        conn.commit()
        logger.info(f"🛡️ [WHITELIST] Marked {qty} {pair} ({side}) as Manual.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to add manual whitelist: {e}")
        return False

def get_manual_whitelists(pair: str = None) -> List[Dict]:
    """Retrieves all active manual whitelists, optionally filtered by pair."""
    try:
        conn = get_connection()
        c = conn.cursor()
        if pair:
            c.execute("SELECT id, pair, side, qty FROM manual_whitelists WHERE pair=?", (pair,))
        else:
            c.execute("SELECT id, pair, side, qty FROM manual_whitelists")
        
        rows = c.fetchall()
        return [{"id": r[0], "pair": r[1], "side": r[2], "qty": r[3]} for r in rows]
    except Exception as e:
        logger.error(f"❌ Failed to fetch manual whitelists: {e}")
        return []

def remove_manual_whitelist(whitelist_id: int):
    """Removes a specific whitelist entry."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM manual_whitelists WHERE id=?", (whitelist_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"❌ Failed to remove manual whitelist {whitelist_id}: {e}")
        return False

def clear_manual_whitelists_for_pair(pair: str):
    """Clears all whitelists for a pair (used when position hits 0)."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM manual_whitelists WHERE pair=?", (pair,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"❌ Failed to clear whitelists for {pair}: {e}")
        return False
