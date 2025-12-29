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
            pair TEXT NOT NULL,
            direction TEXT CHECK(direction IN ('LONG', 'SHORT')) NOT NULL,
            rsi_limit REAL NOT NULL,
            martingale_multiplier REAL NOT NULL,
            base_size REAL NOT NULL,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # Trades table: Tracks active positions and Martingale steps
    # bot_id is a foreign key linked to bots.id
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            bot_id INTEGER PRIMARY KEY,
            current_step INTEGER DEFAULT 0,
            total_invested REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            target_tp_price REAL DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def add_bot(name, pair, direction, rsi_limit, martingale_multiplier, base_size):
    """Adds a new bot and initializes its trade state."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO bots (name, pair, direction, rsi_limit, martingale_multiplier, base_size) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, pair, direction.upper(), rsi_limit, martingale_multiplier, base_size))
        
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

def reset_bot_after_tp(bot_id):
    """Resets the trade stats after a Take Profit (TP) hit."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE trades 
        SET current_step = 0, 
            total_invested = 0, 
            avg_entry_price = 0, 
            target_tp_price = 0
        WHERE bot_id = ?
    ''', (bot_id,))
    conn.commit()
    conn.close()

def get_bot_status(bot_id):
    """Fetches full status joining bot settings and trade data."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT b.name, b.pair, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price 
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE b.id = ?
    ''', (bot_id,))
    result = cursor.fetchone()
    conn.close()
    return result

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
