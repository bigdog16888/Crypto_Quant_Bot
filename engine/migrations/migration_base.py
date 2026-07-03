import sqlite3

class SafeMigration:
    """
    Base class for all migrations that touch bot_orders or trades.
    Enforces INV-33: no migration runs while bots hold positions.
    """
    # Subclasses set this to False only for migrations that do NOT
    # affect open_qty computation (e.g. adding a new column with 
    # DEFAULT value that doesn't touch existing rows)
    requires_flat_positions = True
    
    @classmethod
    def preflight_check(cls, conn):
        if not cls.requires_flat_positions:
            return
        open_bots = conn.execute("""
            SELECT b.id, b.name, t.open_qty
            FROM trades t JOIN bots b ON b.id=t.bot_id
            WHERE t.open_qty > 0.001
        """).fetchall()
        if open_bots:
            bot_list = ', '.join(
                f"{name}({bid})={qty:.4f}" 
                for bid, name, qty in open_bots
            )
            raise RuntimeError(
                f"INV-33 VIOLATED: Cannot run migration while bots "
                f"hold open positions. Close all positions first.\n"
                f"Open bots: {bot_list}\n"
                f"Migration aborted — zero changes made to database."
            )
    
    @classmethod
    def run(cls, conn_or_path):
        if isinstance(conn_or_path, str):
            conn = sqlite3.connect(conn_or_path, check_same_thread=False)
            close_conn = True
        else:
            conn = conn_or_path
            close_conn = False
            
        try:
            # Ensure schema_migrations table exists (for standalone/testing)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     TEXT PRIMARY KEY,
                    applied_at  INTEGER NOT NULL,
                    description TEXT
                )
            """)
            conn.commit()

            version = getattr(cls, 'version', cls.__name__)
            
            # Check if already applied
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (version,)
            ).fetchone()
            
            if applied:
                return  # Already applied — skip entirely, no preflight needed
                
            cls.preflight_check(conn)
            cls._run_impl(conn)
            
            import time
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (version, int(time.time()), getattr(cls, 'description', ''))
            )
            conn.commit()
        finally:
            if close_conn:
                conn.close()
    
    @classmethod
    def _run_impl(cls, conn):
        raise NotImplementedError
