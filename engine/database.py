import sqlite3
import os

DB_PATH = "crypto_bot.db"

def get_connection():
    """Returns a connection to the SQLite database."""
    return sqlite3.connect(DB_PATH)

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    conn = get_connection()
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
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type="MQL4", config_dict=None):
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
    finally:
        conn.close()

def get_bot_params(bot_id):
    """Fetches all configuration parameters for a specific bot."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config FROM bots WHERE id = ?', (bot_id,))
    result = cursor.fetchone()
    conn.close()
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
    finally:
        conn.close()

def update_martingale_step(bot_id, next_step, added_investment, new_avg_price, new_tp_price):
    """Updates the active position stats when a new Martingale step is triggered."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE trades 
        SET current_step = ?, 
            total_invested = total_invested + ?, 
            avg_entry_price = ?, 
            target_tp_price = ?
        WHERE bot_id = ?
    ''', (next_step, added_investment, new_avg_price, new_tp_price, bot_id))
    conn.commit()
    conn.close()

def reset_bot_after_tp(bot_id, exit_price=0):
    """Resets the trade stats after a Take Profit (TP) hit, saving exit metadata."""
    import time
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE trades 
        SET current_step = 0, 
            total_invested = 0, 
            avg_entry_price = 0, 
            target_tp_price = 0,
            last_exit_price = ?,
            last_exit_time = ?
        WHERE bot_id = ?
    ''', (exit_price, int(time.time()), bot_id))
    conn.commit()
    conn.close()

def get_bot_status(bot_id):
    """Fetches full status joining bot settings and trade data."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price, t.last_exit_price, t.last_exit_time
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = ?
    ''', (bot_id,))
    result = cursor.fetchone()
    conn.close()
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
    conn.close()
    return results

def toggle_bot_active(bot_id, new_status):
    """Updates the is_active flag for a bot."""
    conn = get_connection()
    cursor = conn.cursor()
    # Ensure status is integer 0 or 1
    status_int = 1 if new_status else 0
    cursor.execute('UPDATE bots SET is_active = ? WHERE id = ?', (status_int, bot_id))
    conn.commit()
    conn.close()

def delete_bot(bot_id):
    """Deletes a bot and its trade history."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Delete from trades first (FK)
        cursor.execute('DELETE FROM trades WHERE bot_id = ?', (bot_id,))
        # Delete from bots
        cursor.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error deleting bot {bot_id}: {e}")
        return False
    finally:
        conn.close()

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
